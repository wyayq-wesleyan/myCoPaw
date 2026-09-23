# -*- coding: utf-8 -*-
"""The chat transcript must come from durable history, not the live window.

Scroll keeps only a recent tail live and evicts older turns into
``history.db``. ``GET /chats/{id}`` used to return ``agent.state.context`` —
the live window — so every evicted turn vanished from the console: the
right-hand anchor count shrank while chatting and "jump to top" landed in the
middle of the conversation, even though nothing was lost on disk.

These tests pin the durable read and its fallbacks. The row-rebuild cases are
pure (no database); the final one uses a real SQLite file, since the SQL is the
part that can silently regress.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from qwenpaw.agents.context.scroll.history import (  # noqa: E402
    HistoryStore,
    _row_to_msg,
)


class _Row(dict):
    """Stand-in for ``sqlite3.Row`` (supports ``row["col"]``)."""

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


def test_load_session_messages_reads_all_rows_in_order(tmp_path) -> None:
    """The real query: every turn, oldest first, scoped to one session."""
    store = HistoryStore(tmp_path / "history.db")
    try:
        conn = store._conn
        for i in range(1, 18):
            blocks = f'[{{"type": "text", "text": "问题{i}", "id": "b{i}"}}]'
            conn.execute(
                "INSERT INTO conversation_history "
                "(session_id, agent_id, kind, role, name, content, blocks, "
                " metadata, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
                (
                    "s1", "agent1", "context_msg", "user", None,
                    f"问题{i}", blocks,
                    '{"qwenpaw_tag": "external_user_query"}',
                ),
            )
        # A different session must not leak in.
        conn.execute(
            "INSERT INTO conversation_history "
            "(session_id, agent_id, kind, role, content, created_at) "
            "VALUES (?,?,?,?,?,datetime('now'))",
            ("s2", "agent1", "context_msg", "user", "别的会话"),
        )
        conn.commit()

        messages = store.load_session_messages("s1", "agent1")

        assert len(messages) == 17, "every durable turn must be returned"
        assert messages[0].get_text_content() == "问题1"
        assert messages[-1].get_text_content() == "问题17"
    finally:
        store.close()


def test_load_session_messages_returns_empty_for_unknown_session(tmp_path) -> None:
    store = HistoryStore(tmp_path / "history.db")
    try:
        assert store.load_session_messages("nope") == []
        assert store.load_session_messages("") == []
    finally:
        store.close()


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
    """Without the scroll strategy there is no durable store to read."""
    import inspect

    from qwenpaw.app.chats import api as chats_api

    source = inspect.getsource(chats_api._load_durable_transcript)
    assert '!= "scroll"' in source
    assert "return None" in source


@pytest.mark.parametrize("bad", [None, "", "  "])
def test_helper_tolerates_missing_session_id(tmp_path, bad) -> None:
    store = HistoryStore(tmp_path / "history.db")
    try:
        assert store.load_session_messages(bad) == []
    finally:
        store.close()


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
