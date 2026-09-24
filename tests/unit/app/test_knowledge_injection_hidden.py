# -*- coding: utf-8 -*-
"""The automatic knowledge-context injection must never render as a user bubble.

``react_agent`` appends the retrieved knowledge block as a ``role="user"``
message named ``knowledge_auto_context``, with **no metadata tag**. The
transcript filter recognised it by that name, but the durable store did not
persist the message name: a rebuilt row fell back to the literal ``"user"``,
the name check stopped matching, and the injected block was rendered as if the
user had typed it.

Measured symptom: opening an old session showed long knowledge excerpts as user
messages ("以下内容来自当前账户可访问的知识空间…来源：…（文件名匹配 [high置信]）"),
cluttering the transcript and, worse, looking like the user's own input.

Two fixes are pinned here: the name is persisted, and recognition has a
structural fallback so rows written before that still get hidden.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentscope.message import Msg, TextBlock  # noqa: E402

from qwenpaw.agents.context.scroll.history import _row_to_msg  # noqa: E402
from qwenpaw.agents.context.scroll.serialize import msg_to_entries  # noqa: E402
from qwenpaw.app.chats.utils import (  # noqa: E402
    _is_synthetic_user_message,
    agentscope_msg_to_message,
)

INJECTION = (
    "以下内容来自当前账户可访问的知识空间，仅在与当前问题相关时使用；"
    "不要把它当作用户上传的当前图片。\n\n"
    "来源：某材料20260630.docx，第 1 页（文件名匹配 [high置信]）\n某材料"
)


def _injection_msg() -> Msg:
    """Exactly what ``react_agent`` appends: user role, tagged, no id."""
    return Msg(
        name="knowledge_auto_context",
        role="user",
        content=[TextBlock(type="text", text=INJECTION)],
        metadata={"qwenpaw_tag": "knowledge_auto_context"},
    )


def _rebuild(msg: Msg) -> Msg:
    """Persist then rebuild one message exactly as the transcript API does."""

    class _Row(dict):
        def __getitem__(self, key):
            return self.get(key)

    entry = msg_to_entries(msg)[0]
    return _row_to_msg(
        _Row(
            seq=1,
            kind=entry.kind,
            role=entry.role,
            name=entry.name,
            content=entry.content,
            blocks=json.dumps(entry.blocks) if entry.blocks else None,
            metadata=json.dumps(entry.metadata or {}),
        ),
    )


def test_injection_is_tagged_so_it_survives_persistence() -> None:
    """The tag is what a rebuilt row keeps; the name is not persisted."""
    msg = _injection_msg()
    assert (
        msg.metadata.get("qwenpaw_tag") == "knowledge_auto_context"
    )
    rebuilt = _rebuild(msg)
    assert rebuilt is not None
    assert _is_synthetic_user_message(rebuilt) is True


def test_injection_is_hidden_after_persist_and_rebuild() -> None:
    rebuilt = _rebuild(_injection_msg())
    assert rebuilt is not None
    assert _is_synthetic_user_message(rebuilt) is True
    assert agentscope_msg_to_message([rebuilt]) == []


def test_injection_is_hidden_even_when_the_name_was_lost() -> None:
    """Legacy rows have name=None; the content marker must still catch them."""
    legacy = Msg(
        name="user",
        role="user",
        content=[TextBlock(type="text", text=INJECTION)],
        metadata={},
    )
    assert _is_synthetic_user_message(legacy) is True
    assert agentscope_msg_to_message([legacy]) == []


def test_a_real_user_message_is_not_hidden() -> None:
    real = Msg(
        name="user",
        role="user",
        content=[TextBlock(
            type="text",
            text="数据分析表中：乌海全被杀案电围电查话单数据...你检查一下进行修复。",
        )],
        metadata={},
    )
    assert _is_synthetic_user_message(real) is False
    assert len(agentscope_msg_to_message([real])) == 1


def test_a_user_quoting_the_marker_mid_text_is_not_hidden() -> None:
    """Only a leading injection header counts, so discussing it stays visible."""
    quoted = Msg(
        name="user",
        role="user",
        content=[TextBlock(
            type="text",
            text="你刚才注入的「以下内容来自当前账户可访问的知识空间」是什么意思？",
        )],
        metadata={},
    )
    assert _is_synthetic_user_message(quoted) is False


def test_real_tool_call_turns_keep_the_call_name() -> None:
    """The name column still describes the tool call for a tool-call turn."""
    call = Msg(
        name="assistant",
        role="assistant",
        content=[
            {
                "type": "tool_call",
                "id": "c1",
                "name": "read_file",
                "input": "{}",
            },
        ],
        metadata={},
    )
    entry = msg_to_entries(call)[0]
    assert entry.name == "read_file"
