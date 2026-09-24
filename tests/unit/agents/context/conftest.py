# -*- coding: utf-8 -*-
"""Fixtures for the scroll history tests, which now run against PostgreSQL.

``HistoryStore`` no longer owns a SQLite file: history lives in the platform's
PostgreSQL store. These tests therefore need a reachable database, and are
skipped — not failed — when there is none, so the suite stays runnable on a
machine without PostgreSQL.

Each test gets its own workspace key, so tests never observe each other's rows
even though they share one table, and teardown removes exactly what the test
created.
"""

from __future__ import annotations

import os
import uuid

import psycopg2
import psycopg2.extras

import pytest

from qwenpaw.agents.context.scroll.history import HistoryStore
from qwenpaw.platform_store import get_conn, is_available, put_conn


def _platform_db_available() -> bool:
    """Whether the platform database can be reached for these tests.

    Set ``QWENPAW_REQUIRE_PLATFORM_DB=1`` to turn the skip into a failure, so CI
    that is *supposed* to have a database cannot silently skip everything.
    """
    try:
        available = is_available()
    except Exception:  # noqa: BLE001 - any failure means "not usable"
        available = False
    if not available and os.environ.get("QWENPAW_REQUIRE_PLATFORM_DB") == "1":
        pytest.fail("QWENPAW_REQUIRE_PLATFORM_DB=1 but PostgreSQL is unreachable")
    return available


requires_platform_db = pytest.mark.skipif(
    not _platform_db_available(),
    reason="needs the platform PostgreSQL store (set QWENPAW_REQUIRE_PLATFORM_DB=1 to require it)",
)


@pytest.fixture
def store() -> HistoryStore:
    """A HistoryStore scoped to a throwaway workspace, cleaned up afterwards.

    A fresh workspace key per test means one shared table without cross-test
    interference.
    """
    workspace_key = f"test-{uuid.uuid4().hex}"
    handle = HistoryStore(f"/tmp/qwenpaw-history-tests/{workspace_key}/history.db")
    try:
        yield handle
    finally:
        handle.close()
        _delete_workspace(handle.workspace_key)


def _delete_workspace(workspace_key: str) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM platform.conversation_history "
                "WHERE workspace_key = %s",
                (workspace_key,),
            )
            cur.execute(
                "DELETE FROM platform.history_seq WHERE workspace_key = %s",
                (workspace_key,),
            )
        conn.commit()
    except Exception:  # noqa: BLE001 - teardown must not mask the real result
        conn.rollback()
    finally:
        put_conn(conn)


def reset_history(store: HistoryStore) -> None:
    """Discard one workspace's history, as if its store had been recreated.

    The SQLite-side self-heal test deleted ``history.db`` to simulate recovery
    from a corrupt file. There is no file to delete now, so clearing the
    workspace's rows expresses the same situation: a surviving manifest pointing
    at an empty history.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM platform.conversation_history "
                "WHERE workspace_key = %s",
                (store.workspace_key,),
            )
            cur.execute(
                "DELETE FROM platform.history_seq WHERE workspace_key = %s",
                (store.workspace_key,),
            )
        conn.commit()
    finally:
        put_conn(conn)


def history_rows(store: HistoryStore, session_id: str) -> list[dict]:
    """Read this workspace's rows back for assertions.

    Tests used to reach into the SQLite connection; a helper keeps that
    inspection in one place instead of spreading raw SQL across the suite.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT seq, session_id, agent_id, kind, role, name, content, "
                "headline, tool_call_id, tool_state, created_at, dedup_key "
                "FROM platform.conversation_history "
                "WHERE workspace_key = %s AND session_id = %s ORDER BY seq",
                (store.workspace_key, session_id),
            )
            columns = [d[0] for d in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        put_conn(conn)


def history_indexes(store: HistoryStore) -> set[str]:
    """Index names present on the history table."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname='platform' AND tablename='conversation_history'",
            )
            return {row[0] for row in cur.fetchall()}
    finally:
        put_conn(conn)


class ScopedHistorySql:
    """Run test SQL against one workspace's history rows.

    The single shared table means unqualified test SQL such as
    ``SELECT ... FROM conversation_history WHERE session_id='s'`` would also see
    every other test's rows. This exposes the same statement shape through a
    temporary view that is already filtered to one workspace, so tests keep
    inspecting rows directly without each one having to remember the scope — and
    without production code growing a test-only accessor.

    SQLite-style ``?`` placeholders are translated to ``%s`` when parameters are
    passed, so the existing statements did not need rewriting.
    """

    def __init__(self, store: HistoryStore) -> None:
        self._store = store
        self._conn = None
        self._schema = f"hist_test_{store.workspace_key[:12]}"

    def _connection(self):
        if self._conn is None:
            conn = get_conn()
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
                # The key is a hex digest, so interpolating it is safe here; a
                # view cannot take a bind parameter.
                cur.execute(
                    f'CREATE OR REPLACE VIEW "{self._schema}".conversation_history '
                    f"AS SELECT * FROM platform.conversation_history "
                    f"WHERE workspace_key = '{self._store.workspace_key}'",
                )
                # Unqualified names in the tests must resolve to this view rather
                # than requiring every statement to name the schema.
                cur.execute(f'SET search_path TO "{self._schema}", public')
            conn.commit()
            self._conn = conn
        return self._conn

    def execute(self, sql: str, params=None):
        conn = self._connection()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql.replace("?", "%s"), params)
            return _MaterializedResult(list(cur.fetchall()), cur.rowcount)

    def close(self) -> None:
        """Drop the view's schema and return the connection to the pool."""
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')
            conn.commit()
        except Exception:  # noqa: BLE001 - teardown must not mask the result
            conn.rollback()
        finally:
            put_conn(conn)


class _Row(dict):
    """One result row addressable by column name *and* by position.

    ``sqlite3.Row`` supported both, and the tests use both, so the adapter has to
    as well — otherwise migrating them would mean rewriting every row access.
    """

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return list(self.values())[key]
        return dict.__getitem__(self, key)


class _MaterializedResult:
    """The subset of a DB cursor the tests use, over already-fetched rows."""

    def __init__(self, rows: list[dict], rowcount: int) -> None:
        self._rows = [_Row(row) for row in rows]
        self.rowcount = rowcount

    def fetchall(self) -> list[_Row]:
        return list(self._rows)

    def fetchone(self) -> _Row | None:
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


@pytest.fixture
def sql(store: HistoryStore) -> ScopedHistorySql:
    """Workspace-scoped SQL access for tests that inspect durable rows."""
    helper = ScopedHistorySql(store)
    try:
        yield helper
    finally:
        helper.close()
