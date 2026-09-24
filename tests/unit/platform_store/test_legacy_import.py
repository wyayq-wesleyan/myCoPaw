# -*- coding: utf-8 -*-
"""The one-time SQLite → PostgreSQL upgrade.

A deployment upgrading from the SQLite era still has ``history.db`` files and a
global ``governance/audit.db`` on disk. Those tables now live in PostgreSQL, so
the contents have to be carried over — otherwise recalled history and the audit
trail silently start from empty.

These tests build real legacy SQLite files with the old schema and drive the
migration against the real PostgreSQL store.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid

from pathlib import Path

import pytest

from qwenpaw.agents.context.scroll.history import HistoryStore
from qwenpaw.agents.context.types import LogEntry
from qwenpaw.platform_store import get_conn, put_conn
from qwenpaw.platform_store.legacy_import import (
    import_legacy_audit,
    import_legacy_history,
)

# Reuse the scroll suite's database-availability gate so a machine without
# PostgreSQL skips rather than fails.
from ..agents.context.conftest import requires_platform_db  # noqa: E402

_LEGACY_HISTORY_DDL = """
CREATE TABLE conversation_history (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    agent_id     TEXT,
    kind         TEXT NOT NULL,
    role         TEXT,
    name         TEXT,
    content      TEXT,
    tool_call_id TEXT,
    tool_input   TEXT,
    tool_state   TEXT,
    headline     TEXT,
    blocks       TEXT,
    metadata     TEXT,
    created_at   TEXT,
    dedup_key    TEXT
)
"""

_LEGACY_AUDIT_DDL = """
CREATE TABLE audit_events (
    ts            INTEGER NOT NULL,
    workspace_dir TEXT NOT NULL,
    agent_id      TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    tool_name     TEXT NOT NULL,
    target        TEXT NOT NULL,
    decision      TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    extra         TEXT NOT NULL DEFAULT '{}'
)
"""

_INSERT_HISTORY = (
    "INSERT INTO conversation_history "
    "(session_id, agent_id, kind, role, name, content, tool_call_id, "
    "tool_input, tool_state, headline, blocks, metadata, created_at, "
    "dedup_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _legacy_history_db(path: Path, rows: list[tuple]) -> Path:
    """Build a ``history.db`` with the exact schema the old store created."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(_LEGACY_HISTORY_DDL)
        for row in rows:
            conn.execute(_INSERT_HISTORY, row)
        conn.commit()
    finally:
        conn.close()
    return path


def _legacy_audit_db(path: Path, rows: list[tuple]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(_LEGACY_AUDIT_DDL)
        conn.executemany(
            "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _workspace_row(
    session_id="s1",
    agent_id="ag1",
    kind="model_turn",
    role="assistant",
    content="legacy content",
    headline=None,
    blocks=None,
    metadata=None,
    created_at="2026-01-01T00:00:00+00:00",
    dedup_key="d1",
    name=None,
):
    return (
        session_id, agent_id, kind, role, name, content, None, None, None,
        headline, blocks, metadata, created_at, dedup_key,
    )


@pytest.fixture
def legacy(tmp_path) -> tuple[HistoryStore, Path]:
    """A legacy history.db plus a store bound to the same workspace path."""
    db = _legacy_history_db(
        tmp_path / "history.db",
        [
            _workspace_row(
                session_id="s1", content="legacy question", kind="context_msg",
                role="user", headline=None, dedup_key="u1",
            ),
            _workspace_row(
                session_id="s1", content="legacy answer", headline="head",
                blocks='[{"t":1}]', metadata='{"k":"v"}', dedup_key="a1",
            ),
            _workspace_row(
                session_id="s2", content="other session", dedup_key="o1",
            ),
        ],
    )
    store = HistoryStore(db)
    try:
        yield store, db
    finally:
        _cleanup(store.workspace_key, [db])


def _cleanup(workspace_key: str, sources: list[Path]) -> None:
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
            for source in sources:
                cur.execute(
                    "DELETE FROM platform.legacy_import_state "
                    "WHERE source_path = %s",
                    (str(source),),
                )
        conn.commit()
    except Exception:  # noqa: BLE001 - teardown must not mask the result
        conn.rollback()
    finally:
        put_conn(conn)


@requires_platform_db
def test_history_rows_keep_their_original_seq(legacy):
    """The load-bearing guarantee: ``seq`` must not be renumbered.

    The eviction index stores ``seq`` ranges inside session state, so renumbering
    imported history would invalidate every existing reference to it.
    """
    store, db = legacy
    result = import_legacy_history(store, db)
    assert result.rows == 3
    assert result.error is None

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT seq, session_id, content, headline, blocks, metadata "
                "FROM platform.conversation_history "
                "WHERE workspace_key = %s ORDER BY seq",
                (store.workspace_key,),
            )
            rows = cur.fetchall()
    finally:
        put_conn(conn)

    assert [row[0] for row in rows] == [1, 2, 3]
    assert rows[1] == (2, "s1", "legacy answer", "head", '[{"t":1}]', '{"k":"v"}')


@requires_platform_db
def test_import_is_idempotent(legacy):
    """A second pass must not duplicate rows — an upgrade can be retried."""
    store, db = legacy
    assert import_legacy_history(store, db).rows == 3

    second = import_legacy_history(store, db)
    assert second.rows == 0
    assert second.skipped is True  # recognised by checksum

    # Even bypassing the checksum skip, nothing is duplicated.
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM platform.conversation_history "
                "WHERE workspace_key = %s",
                (store.workspace_key,),
            )
            assert cur.fetchone()[0] == 3
    finally:
        put_conn(conn)


@requires_platform_db
def test_append_continues_after_imported_rows(legacy):
    """The watermark must move past the imported maximum.

    If it did not, the next ordinary ``append`` would allocate an already-taken
    ``seq`` and fail on the primary key.
    """
    store, db = legacy
    import_legacy_history(store, db)

    seq = store.append(
        session_id="s1",
        agent_id="ag1",
        dedup_key="post-import",
        entry=LogEntry(
            kind="model_turn",
            role="assistant",
            content="written after the import",
        ),
    )
    assert seq == 4


@requires_platform_db
def test_resumed_import_fills_only_what_is_missing(tmp_path):
    """An interrupted file (rows present, bookkeeping absent) resumes.

    That is the state a crash mid-import leaves behind: some rows committed, no
    ``done`` record. The retry must complete the file without duplicating the
    rows that already landed.
    """
    db = _legacy_history_db(
        tmp_path / "history.db",
        [
            _workspace_row(session_id="s1", content="first", dedup_key="r1"),
            _workspace_row(session_id="s1", content="second", dedup_key="r2"),
        ],
    )
    store = HistoryStore(db)
    try:
        # Simulate the partial state: insert row 1 only, with no bookkeeping.
        store.import_rows(
            [
                {
                    "seq": 1,
                    "session_id": "s1",
                    "agent_id": "ag1",
                    "kind": "model_turn",
                    "role": "assistant",
                    "name": None,
                    "content": "first",
                    "tool_call_id": None,
                    "tool_input": None,
                    "tool_state": None,
                    "headline": None,
                    "blocks": None,
                    "metadata": None,
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "dedup_key": "r1",
                },
            ],
        )
        result = import_legacy_history(store, db)
        # Only the missing row was inserted.
        assert result.rows == 1

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT seq, content FROM platform.conversation_history "
                    "WHERE workspace_key = %s ORDER BY seq",
                    (store.workspace_key,),
                )
                rows = cur.fetchall()
        finally:
            put_conn(conn)
        assert rows == [(1, "first"), (2, "second")]
    finally:
        _cleanup(store.workspace_key, [db])


@requires_platform_db
def test_import_of_absent_table_is_a_no_op(tmp_path):
    """A legacy file that predates the feature yields nothing, not an error."""
    db = tmp_path / "history.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()

    store = HistoryStore(db)
    try:
        result = import_legacy_history(store, db)
        assert result.rows == 0
        assert result.error is None
    finally:
        _cleanup(store.workspace_key, [db])


@requires_platform_db
def test_legacy_audit_rows_are_imported_once(tmp_path):
    audit_db = _legacy_audit_db(
        tmp_path / "governance" / "audit.db",
        [
            (1767225600000, str(tmp_path), "ag1", "audit-import-s1",
             "Bash", "git status", "allow", "ok", "{}"),
        ],
    )
    session_marker = "audit-import-s1"
    try:
        first = import_legacy_audit(audit_db)
        assert first.rows == 1

        # Idempotent: the checksum makes the second pass a skip.
        second = import_legacy_audit(audit_db)
        assert second.skipped is True

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ts, tool_name, decision FROM platform.audit_events "
                    "WHERE session_id = %s",
                    (session_marker,),
                )
                rows = cur.fetchall()
        finally:
            put_conn(conn)
        assert rows == [(1767225600000.0, "Bash", "allow")]
    finally:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM platform.audit_events WHERE session_id = %s",
                    (session_marker,),
                )
                cur.execute(
                    "DELETE FROM platform.legacy_import_state "
                    "WHERE source_path = %s",
                    (str(audit_db),),
                )
            conn.commit()
        finally:
            put_conn(conn)


@requires_platform_db
def test_legacy_token_usage_is_imported_and_adds_up(tmp_path):
    """The usage JSON's numbers must land exactly, account breakdown included.

    The import is additive rather than max-based, so a deployment that keeps
    recording usage after the upgrade ends up with the historical totals plus
    the new ones — not whichever figure happened to be larger.
    """
    import json

    from qwenpaw.platform_store.legacy_import import import_legacy_token_usage
    from qwenpaw.token_usage.storage import load_data, save_data_sync

    path = tmp_path / "token_usage.json"
    path.write_text(
        json.dumps(
            {
                "2026-03-01": {
                    "openai:legacy-tu-probe": {
                        "provider_id": "openai",
                        "model_name": "legacy-tu-probe",
                        "prompt_tokens": 1000,
                        "completion_tokens": 200,
                        "call_count": 4,
                        "by_user": {
                            "alice": {
                                "prompt_tokens": 700,
                                "completion_tokens": 150,
                                "call_count": 3,
                            },
                            "bob": {
                                "prompt_tokens": 300,
                                "completion_tokens": 50,
                                "call_count": 1,
                            },
                        },
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    def _cleanup():
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM platform.token_usage "
                    "WHERE model_name = 'legacy-tu-probe'",
                )
                cur.execute(
                    "DELETE FROM platform.legacy_import_state "
                    "WHERE source_path = %s",
                    (str(path),),
                )
            conn.commit()
        finally:
            put_conn(conn)

    _cleanup()
    try:
        first = import_legacy_token_usage(path)
        assert first.error is None
        assert first.rows == 3  # alice + bob + unattributed(0)

        # Idempotent by checksum, and re-importing would double anyway, so the
        # skip is what protects the totals.
        assert import_legacy_token_usage(path).skipped is True

        document = asyncio.run(load_data())
        entry = document["2026-03-01"]["openai:legacy-tu-probe"]
        assert entry["prompt_tokens"] == 1000
        assert entry["completion_tokens"] == 200
        assert entry["call_count"] == 4
        assert entry["by_user"]["alice"]["prompt_tokens"] == 700
        assert entry["by_user"]["bob"]["prompt_tokens"] == 300

        # New usage recorded after the upgrade adds to the imported totals.
        assert save_data_sync(
            None,
            {
                "2026-03-01": {
                    "openai:legacy-tu-probe": {
                        "provider_id": "openai",
                        "model_name": "legacy-tu-probe",
                        "prompt_tokens": 1010,
                        "completion_tokens": 205,
                        "call_count": 5,
                        "by_user": {
                            "alice": {
                                "prompt_tokens": 710,
                                "completion_tokens": 155,
                                "call_count": 4,
                            },
                            "bob": {
                                "prompt_tokens": 300,
                                "completion_tokens": 50,
                                "call_count": 1,
                            },
                        },
                    },
                },
            },
        ) is True

        after = asyncio.run(load_data())["2026-03-01"][
            "openai:legacy-tu-probe"
        ]
        # The larger, newer snapshot wins the maxima — the imported total is not
        # clobbered back down.
        assert after["prompt_tokens"] == 1010
        assert after["call_count"] == 5
    finally:
        _cleanup()
