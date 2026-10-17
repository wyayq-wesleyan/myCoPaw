# -*- coding: utf-8 -*-
"""The chat transcript must come from durable history, not the live window.

Scroll keeps only a recent tail live and evicts older turns into durable
storage. ``GET /chats/{id}`` used to return ``agent.state.context`` — the live
window — so every evicted turn vanished from the console: the right-hand anchor
count shrank while chatting and "jump to top" landed in the middle of the
conversation, even though nothing was lost on disk.

These tests pin the durable read and its fallbacks. The row-rebuild and
timestamp cases are pure (no database); the durable-read cases run against the
platform's PostgreSQL store and skip — not fail — when none is reachable (the
deeper store coverage lives in ``tests/unit/agents/context/test_history_store.py``).
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from qwenpaw.agents.context.scroll.history import (  # noqa: E402
    HistoryStore,
    _row_to_msg,
)
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
    """A HistoryStore scoped to a throwaway workspace, cleaned up afterwards.

    A fresh workspace key per test means one shared table without cross-test
    interference.
    """
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


class _Row(dict):
    """Stand-in for a durable result row (supports ``row["col"]``)."""

    def __getitem__(self, key):
        return self.get(key)


def _row(**over):
    base = {
        "seq": 1,
        "kind": "context_msg",
        "role": "user",
        "name": None,
        "content": "你好",
        "blocks": '[{"type": "text", "text": "你好", "id": "b1"}]',
        "metadata": "{}",
        "created_at": None,
    }
    base.update(over)
    return _Row(**base)


def test_row_rebuilds_into_a_msg() -> None:
    msg = _row_to_msg(_row())
    assert msg is not None
    assert msg.role == "user"
    assert msg.get_text_content() == "你好"


def test_row_falls_back_to_content_when_blocks_missing() -> None:
    """A row without ``blocks`` still shows its flattened text."""
    msg = _row_to_msg(_row(blocks=None, content="only text"))
    assert msg is not None
    assert msg.get_text_content() == "only text"


def test_broken_row_is_skipped_not_raised() -> None:
    """One unparseable row must not blank the whole conversation."""
    assert _row_to_msg(_row(blocks="not json at all", content="")) is not None
    # A row missing its role entirely still yields something usable.
    assert _row_to_msg(_row(role=None, name=None)) is not None


def test_tool_result_blocks_survive_reconstruction() -> None:
    """blocks hold a faithful dump, so tool calls must not degrade to text."""
    # ``ToolCallBlock.input`` is a JSON *string* in AgentScope 2.x, which is
    # exactly what ``model_dump`` stores, so the stored shape is the real one.
    blocks = (
        '[{"type": "tool_call", "id": "c1", "name": "read_file",'
        ' "input": "{\\"path\\": \\"a.txt\\"}"}]'
    )
    msg = _row_to_msg(_row(role="assistant", blocks=blocks, content=""))
    assert msg is not None
    types = [getattr(b, "type", None) for b in msg.content]
    assert "tool_call" in types


@requires_platform_db
def test_load_session_messages_reads_all_rows_in_order(pg_store) -> None:
    """The real query: every turn, oldest first, scoped to one session."""
    for i in range(1, 18):
        pg_store.append(
            session_id="s1",
            agent_id="agent1",
            dedup_key=f"m{i}",
            entry=LogEntry(
                kind="context_msg",
                role="user",
                content=f"问题{i}",
                blocks=[{"type": "text", "text": f"问题{i}", "id": f"b{i}"}],
                metadata={"qwenpaw_tag": "external_user_query"},
            ),
        )
    # A different session must not leak in.
    pg_store.append(
        session_id="s2",
        agent_id="agent1",
        dedup_key="other",
        entry=LogEntry(kind="context_msg", role="user", content="别的会话"),
    )

    messages = pg_store.load_session_messages("s1", "agent1")

    assert len(messages) == 17, "every durable turn must be returned"
    assert messages[0].get_text_content() == "问题1"
    assert messages[-1].get_text_content() == "问题17"


@requires_platform_db
def test_load_session_messages_returns_empty_for_unknown_session(pg_store):
    assert pg_store.load_session_messages("nope") == []
    assert pg_store.load_session_messages("") == []


def test_api_prefers_the_durable_transcript_over_the_window() -> None:
    """``get_chat`` must try durable history before falling back."""
    import inspect

    from qwenpaw.app.chats import api as chats_api

    source = inspect.getsource(chats_api.get_chat)
    assert "_load_durable_transcript" in source
    # The window must remain the fallback, not the primary source.
    assert source.index("_load_durable_transcript") < source.index(
        "agent_state.context",
    )


def test_non_scroll_strategy_has_no_transcript() -> None:
    """Without the scroll strategy there is no durable store to read.

    Asserted as **behaviour**, not as source text. The strategy check now lives in
    ``_scroll_history_store_path`` because the export path shares it, so the old
    ``'!= "scroll"' in source`` form failed on a refactor that kept the behaviour
    identical — and, worse, it would have passed had the check merely been
    relocated without ever being applied.
    """
    from types import SimpleNamespace
    from unittest.mock import patch

    from qwenpaw.app.chats import api as chats_api

    workspace = SimpleNamespace(agent_id="default", workspace_dir="/tmp/unused")
    spec = SimpleNamespace(id="chat-1", session_id="console:u:1")

    def _agent_config(strategy):
        lcc = SimpleNamespace(
            strategy=strategy,
            scroll_config=SimpleNamespace(db_filename="history.db"),
        )
        return SimpleNamespace(running=SimpleNamespace(light_context_config=lcc))

    for strategy in ("native", None, "compact"):
        with patch(
            "qwenpaw.config.config.load_agent_config",
            return_value=_agent_config(strategy),
        ), patch(
            "qwenpaw.agents.context.scroll.history.HistoryStore",
        ) as store_cls:
            assert chats_api._load_durable_transcript(workspace, spec) is None
            assert not store_cls.called, (
                f"strategy={strategy!r} 不是 scroll，却去读了历史存储"
            )


@requires_platform_db
@pytest.mark.parametrize("bad", [None, "", "  "])
def test_helper_tolerates_missing_session_id(pg_store, bad) -> None:
    assert pg_store.load_session_messages(bad) == []


def test_durable_read_precedes_the_missing_state_early_return() -> None:
    """A chat with no state file must still show its durable transcript.

    The state file is rotated away or lost across restarts, while history.db
    keeps the conversation. Reading the window first meant an early
    ``if not state: return []`` answered "no messages" for a chat whose content
    was fully intact on disk — the transcript simply looked empty.
    """
    import inspect

    from qwenpaw.app.chats import api as chats_api

    source = inspect.getsource(chats_api.get_chat)
    assert source.index("_load_durable_transcript") < source.index("if not state:")


def test_oversized_tool_results_are_capped() -> None:
    """A tool result must not dominate the transcript response.

    Returning the whole durable transcript made the payload unbounded: tool
    outputs are routinely tens of KB, so 500 such turns produced a 19MB
    response that the console had to download and parse (measured). The
    transcript only needs enough to recognise the call.
    """
    from qwenpaw.agents.context.scroll.history import (
        _TRANSCRIPT_TOOL_RESULT_MAX_CHARS,
        _TRANSCRIPT_TRUNCATED_MARK,
        _row_to_msg,
    )

    huge = "X" * 200_000
    blocks = (
        '[{"type": "tool_call", "id": "c1", "name": "read_file",'
        ' "input": "{}"},'
        ' {"type": "tool_result", "id": "c1", "name": "read_file",'
        f' "output": [{{"type": "text", "text": "{huge}"}}],'
        ' "state": "success"}]'
    )
    msg = _row_to_msg(_row(role="assistant", blocks=blocks, content=""))

    assert msg is not None
    # ``get_text_content()`` deliberately ignores tool results, so assert on the
    # block's own output.
    results = [b for b in msg.content if getattr(b, "type", None) == "tool_result"]
    assert results, "the tool result block was dropped"
    rendered = results[0].output[0].text
    assert len(rendered) < 20_000, f"tool output was not capped ({len(rendered)})"
    assert _TRANSCRIPT_TRUNCATED_MARK.strip() in rendered
    # The call itself must survive so the card still has something to show.
    assert any(getattr(b, "type", None) == "tool_call" for b in msg.content)


def test_normal_sized_tool_results_are_untouched() -> None:
    """Small results must not be altered, or cards would lose their content."""
    from qwenpaw.agents.context.scroll.history import _row_to_msg

    blocks = (
        '[{"type": "tool_result", "id": "c1", "name": "read_file",'
        ' "output": [{"type": "text", "text": "short output"}],'
        ' "state": "success"}]'
    )
    msg = _row_to_msg(_row(role="assistant", blocks=blocks, content=""))
    assert msg is not None
    results = [b for b in msg.content if getattr(b, "type", None) == "tool_result"]
    assert results[0].output[0].text == "short output"


# --------------------------------------------------------- message timestamps


def test_rebuilt_rows_carry_their_timestamp() -> None:
    """A rebuilt message must keep its time.

    The console reads a message's time from ``metadata.timestamp`` and
    ``Msg.timestamp`` aliases ``created_at``. Dropping both meant every rebuilt
    message had no time, so a turn's request and its reply could not be ordered
    against each other and the question re-appeared *below* the answer after a
    page refresh.
    """
    user = _row_to_msg(
        _row(role="user", created_at="2026-09-24T09:16:27.211129"),
    )
    assistant = _row_to_msg(
        _row(role="assistant", created_at="2026-09-24T09:17:11.652462"),
    )

    assert user.created_at == "2026-09-24T09:16:27.211129"
    assert assistant.created_at == "2026-09-24T09:17:11.652462"
    for msg in (user, assistant):
        assert (msg.metadata or {}).get("timestamp"), (
            "metadata.timestamp drives the console's clock"
        )


def test_existing_metadata_timestamp_wins() -> None:
    """A stored timestamp must not be overwritten by the row's created_at."""
    msg = _row_to_msg(
        _row(
            metadata='{"timestamp": "2020-01-01T00:00:00"}',
            created_at="2026-09-24T09:16:27.211129",
        ),
    )

    assert msg.metadata["timestamp"] == "2020-01-01T00:00:00"


@requires_platform_db
def test_transcript_is_ordered_by_time_not_write_order(pg_store) -> None:
    """A turn written later but happening earlier must still sort first.

    ``seq`` is the *write* order and it can disagree with the conversation's
    order — a resumed/retried turn, or rows imported by the session-sync pass,
    can land after a turn that chronologically follows it. Sorting by ``seq``
    therefore rendered a question *below* its answer once the transcript came
    from the durable store (the live window never showed this).
    """
    # Write them out of order on purpose: the assistant row gets the lower
    # seq even though it happened later in time.
    pg_store.append(
        session_id="s1",
        agent_id="a1",
        dedup_key="u2",
        entry=LogEntry(
            kind="model_turn",
            role="assistant",
            name="assistant",
            content="答案",
            blocks=[{"type": "text", "text": "答案"}],
            created_at="2026-09-24T09:26:52",
        ),
    )
    pg_store.append(
        session_id="s1",
        agent_id="a1",
        dedup_key="u1",
        entry=LogEntry(
            kind="context_msg",
            role="user",
            name="user",
            content="问题",
            blocks=[{"type": "text", "text": "问题"}],
            created_at="2026-09-24T09:25:59",
        ),
    )

    messages = pg_store.load_session_messages("s1", "a1")

    assert [m.role for m in messages] == ["user", "assistant"], (
        "transcript must follow time, not the order rows were written"
    )


@requires_platform_db
def test_same_timestamp_rows_keep_write_order(pg_store) -> None:
    """Ties break on seq, so a turn's internal order is preserved.

    Every row of one turn shares its message's ``created_at``; ``seq`` must
    therefore still decide the order *within* a turn (tool call before result).
    """
    same = "2026-09-24T09:26:52"
    pg_store.append(
        session_id="s1",
        agent_id="a1",
        dedup_key="a",
        entry=LogEntry(
            kind="model_turn",
            role="assistant",
            name="assistant",
            content="first",
            blocks=[{"type": "text", "text": "first"}],
            created_at=same,
        ),
    )
    pg_store.append(
        session_id="s1",
        agent_id="a1",
        dedup_key="b",
        entry=LogEntry(
            kind="model_turn",
            role="assistant",
            name="assistant",
            content="second",
            blocks=[{"type": "text", "text": "second"}],
            created_at=same,
        ),
    )

    messages = pg_store.load_session_messages("s1", "a1")

    assert [m.get_text_content() for m in messages] == ["first", "second"]


@requires_platform_db
def test_undated_rows_fall_last_not_first(pg_store) -> None:
    """A row with no timestamp must not jump to the top of the transcript.

    SQLite sorted ``NULL`` first, so ordering on ``created_at`` alone pushed any
    undated row (legacy data, a partially written turn) ahead of the whole
    conversation. ``created_at IS NULL`` is sorted on last so those rows trail
    instead, keeping their relative write order.
    """
    rows = [
        ("user", "dated-question", "2026-09-24T09:00:00", "a"),
        ("assistant", "dated-answer", "2026-09-24T09:01:00", "b"),
    ]
    for role, text, when, key in rows:
        pg_store.append(
            session_id="s1",
            agent_id="a1",
            dedup_key=key,
            entry=LogEntry(
                kind="context_msg",
                role=role,
                name=role,
                content=text,
                blocks=[{"type": "text", "text": text}],
                created_at=when,
            ),
        )
    # ``append`` always stamps ``created_at``, so the undated legacy row is
    # written directly.
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO platform.conversation_history "
                "(workspace_key, seq, session_id, kind, role, name, content, "
                " created_at, dedup_key) "
                "VALUES (%s, 3, 's1', 'context_msg', 'assistant', NULL, "
                " 'undated-old', NULL, 'c')",
                (pg_store.workspace_key,),
            )
        conn.commit()
    finally:
        put_conn(conn)

    messages = pg_store.load_session_messages("s1", "a1")

    assert [m.get_text_content() for m in messages] == [
        "dated-question",
        "dated-answer",
        "undated-old",
    ], "an undated row must not be placed before the dated conversation"
