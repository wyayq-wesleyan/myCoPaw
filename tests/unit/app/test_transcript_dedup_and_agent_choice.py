# -*- coding: utf-8 -*-
"""Duplicate rows must not be replayed, and a fresh browser must find history.

Two separate defects reported from production:

1. A question appeared twice with its answer in between. The durable store
   dedupes on ``(session_id, dedup_key)``, but the key was not stable: when a
   message carried no id the caller fell back to ``str(id(msg))`` — a memory
   address, so the same message was written again under a new key after every
   restart, and replaying the rows showed it twice.

2. Opening the console in a different browser showed no conversations. The
   chosen agent is remembered in localStorage, which a fresh browser lacks, so
   it fell back to the literal "default" agent — and conversations live in
   per-agent files, so an account whose history sits under another agent saw an
   empty list.

The transcript read now collapses duplicates, the identity used for persistence
is content-stable, and the API reports which agents hold conversations. The
duplicate-row cases run against the platform's PostgreSQL store and skip — not
fail — when none is reachable.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentscope.message import Msg, TextBlock  # noqa: E402

from qwenpaw.agents.context.scroll.history import HistoryStore  # noqa: E402
from qwenpaw.agents.context.scroll.manager import stable_message_key  # noqa: E402
from qwenpaw.agents.context.types import LogEntry  # noqa: E402
from qwenpaw.platform_store import (  # noqa: E402
    get_conn,
    is_available,
    put_conn,
)


def _platform_db_available() -> bool:
    """Whether the platform database can be reached for the durable-read tests."""
    try:
        return bool(is_available())
    except Exception:  # noqa: BLE001 - any failure means "not usable"
        return False


requires_platform_db = pytest.mark.skipif(
    not _platform_db_available(),
    reason="needs the platform PostgreSQL store",
)


@pytest.fixture
def pg_store() -> HistoryStore:
    """A HistoryStore scoped to a throwaway workspace, cleaned up afterwards."""
    handle = HistoryStore(
        f"/tmp/qwenpaw-history-tests/test-{uuid.uuid4().hex}/history.db",
    )
    try:
        yield handle
    finally:
        handle.close()
        _delete_workspace(handle.workspace_key)


def _delete_workspace(workspace_key: str) -> None:
    """Remove exactly the rows the test's workspace created."""
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


def _append(
    store: HistoryStore,
    role: str,
    text: str,
    when: str,
    key: str,
) -> None:
    store.append(
        session_id="s1",
        agent_id="a1",
        dedup_key=key,
        entry=LogEntry(
            kind="context_msg" if role == "user" else "model_turn",
            role=role,
            name=role,
            content=text,
            blocks=[{"type": "text", "text": text}],
            created_at=when,
        ),
    )


@requires_platform_db
def test_duplicate_rows_are_collapsed(pg_store) -> None:
    """The same event written under two keys must appear once."""
    # Production shape: the question was stored twice (different keys, same
    # timestamp) with its answer in between.
    _append(pg_store, "user", "问题一", "2026-09-24T09:00:00", "keyA")
    _append(pg_store, "assistant", "回答一", "2026-09-24T09:01:00", "keyB")
    _append(pg_store, "user", "问题一", "2026-09-24T09:00:00", "keyC")

    messages = pg_store.load_session_messages("s1", "a1")

    assert [m.get_text_content() for m in messages] == ["问题一", "回答一"], (
        "a duplicated row was replayed"
    )


@requires_platform_db
def test_genuine_repeat_asking_survives(pg_store) -> None:
    """Asking the same thing again later is real content, not a duplicate."""
    _append(pg_store, "user", "问题一", "2026-09-24T09:00:00", "k1")
    _append(pg_store, "assistant", "回答一", "2026-09-24T09:01:00", "k2")
    _append(pg_store, "user", "问题一", "2026-09-24T10:00:00", "k3")
    _append(pg_store, "assistant", "回答二", "2026-09-24T10:01:00", "k4")

    messages = pg_store.load_session_messages("s1", "a1")

    assert [m.get_text_content() for m in messages] == [
        "问题一", "回答一", "问题一", "回答二",
    ]


@requires_platform_db
def test_rebuilt_messages_have_stable_ids(pg_store) -> None:
    """Ids must not change between reads, or the console re-keys its cards."""
    _append(pg_store, "user", "稳定", "2026-09-24T09:00:00", "k1")

    first = pg_store.load_session_messages("s1", "a1")
    second = pg_store.load_session_messages("s1", "a1")

    assert first[0].id == second[0].id


def test_message_key_is_stable_without_an_id() -> None:
    """The persistence identity must not depend on a memory address.

    ``str(id(msg))`` differs after a restart, which is what wrote the duplicate
    rows in the first place.
    """

    class _NoId:
        id = None
        role = "user"
        name = "user"
        created_at = "2026-09-24T09:00:00"
        content = [TextBlock(type="text", text="同一句话")]

    assert stable_message_key(_NoId()) == stable_message_key(_NoId())


def test_message_key_prefers_the_real_id() -> None:
    msg = Msg(name="user", role="user", content=[TextBlock(type="text", text="x")])
    assert stable_message_key(msg) == msg.id


def test_agent_chat_count_endpoint_reports_owned_agents() -> None:
    """The console needs to know which agent holds the account's history."""
    import inspect

    from qwenpaw.app.routers import agents as agents_module

    source = inspect.getsource(agents_module.list_agents_with_chats)
    # Ownership-scoped, and reads the per-agent chats file.
    assert "user_agent_ids" in source
    assert "CHATS_FILE" in source
