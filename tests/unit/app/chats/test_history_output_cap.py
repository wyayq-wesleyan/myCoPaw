# -*- coding: utf-8 -*-
"""服务聊天历史时截断超大工具输出（#8229）。

为什么：截图、``send_file`` 的产物会以 base64 持久化在 ``tool_result`` 里。
原样返回时单次历史响应可以到 10MB+，在慢链路上必然撞上控制台 30 秒超时。

三个约束：
- 只在**读历史**路径开启（默认 False），流式路径与其他调用方字节级不变；
- 截断必须是**结构化**的：绝不切序列化后的 JSON 字符串（会产生非法 JSON）；
- 两条历史出口（durable transcript / live window）都要开启，否则默认的
  Scroll 路径仍然返回超大响应。

关于 dict：``ToolResultBlock.output`` 被 AgentScope 限定为 ``str | list``，
所有历史路径都会经过 Msg 校验，所以服务历史时**不会**出现 dict 形态。
``_cap_tool_output`` 仍处理 dict，属于纵深防御（未来若绕过 Msg 也不失效），
对应的用例直接打在函数上，不假装它来自 Msg。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from agentscope.message import (
    Base64Source,
    DataBlock,
    Msg,
    TextBlock,
    ToolResultBlock,
)

from qwenpaw.app.chats.utils import (
    _BASE64_KEEP_CHARS,
    _MAX_TOOL_OUTPUT_CHARS,
    _cap_tool_output,
    agentscope_msg_to_message,
)


def _tool_result_msg(output) -> Msg:
    return Msg(
        name="tool",
        role="assistant",
        content=[
            ToolResultBlock(
                id="call-1",
                name="send_file_to_user",
                output=output,
                state="success",
            ),
        ],
    )


def _served_output(msg: Msg, *, truncate: bool) -> str:
    [message] = agentscope_msg_to_message(
        msg,
        truncate_oversized_outputs=truncate,
    )
    return message.content[0].data["output"]


def _strip_ids(value):
    """Drop the random block ids AgentScope adds when coercing dicts."""
    if isinstance(value, list):
        return [_strip_ids(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _strip_ids(item)
            for key, item in value.items()
            if key != "id"
        }
    return value


# ---------------------------------------------------------------------------
# 复现与主路径
# ---------------------------------------------------------------------------


def test_list_with_large_base64_is_shrunk() -> None:
    """修复前：这条会把 2MB base64 原样返回；修复后必须显著变小。"""
    big = "A" * (2 * 1024 * 1024)
    output = [
        DataBlock(
            type="data",
            source=Base64Source(
                type="base64",
                media_type="image/png",
                data=big,
            ),
            name="screenshot.png",
        ),
        TextBlock(type="text", text="done"),
    ]
    served = _served_output(_tool_result_msg(output), truncate=True)

    assert len(served) < 100 * 1024, f"still {len(served)} chars"
    assert "base64 omitted" in served
    assert "done" in served
    json.loads(served)  # 必须是合法 JSON


def test_long_string_is_truncated_with_marker() -> None:
    served = _served_output(
        _tool_result_msg("x" * 5_000_000),
        truncate=True,
    )
    assert len(served) <= _MAX_TOOL_OUTPUT_CHARS + 100
    assert "[output truncated: 5000000 chars]" in served


# ---------------------------------------------------------------------------
# dict 形态：纵深防御（当前不来自 Msg）
# ---------------------------------------------------------------------------


def test_dict_output_is_capped_directly() -> None:
    capped = _cap_tool_output(
        {"blob": "y" * 5_000_000, "ok": True},
        _MAX_TOOL_OUTPUT_CHARS,
    )
    serialised = json.dumps(capped, ensure_ascii=False)
    assert len(serialised) < 100 * 1024
    assert json.loads(serialised)["ok"] is True


def test_dict_nested_base64_is_replaced_directly() -> None:
    payload = {
        "content": [
            {
                "type": "data",
                "source": {"type": "base64", "data": "B" * 200_000},
            },
        ],
    }
    serialised = json.dumps(
        _cap_tool_output(payload, _MAX_TOOL_OUTPUT_CHARS),
        ensure_ascii=False,
    )
    assert "base64 omitted" in serialised
    assert "B" * 1000 not in serialised


# ---------------------------------------------------------------------------
# 不改变正常行为
# ---------------------------------------------------------------------------


def test_small_output_is_unchanged() -> None:
    output = [TextBlock(type="text", text="ok")]
    with_cap = _served_output(_tool_result_msg(output), truncate=True)
    without_cap = _served_output(_tool_result_msg(output), truncate=False)
    assert _strip_ids(json.loads(with_cap)) == _strip_ids(json.loads(without_cap))


def test_default_is_off() -> None:
    """流式与其他调用方不传参数时必须保持原样。"""
    big = "z" * (_MAX_TOOL_OUTPUT_CHARS * 3)
    [message] = agentscope_msg_to_message(_tool_result_msg(big))
    assert message.content[0].data["output"] == big


def test_base64_below_threshold_is_kept() -> None:
    small = "C" * (_BASE64_KEEP_CHARS // 2)
    output = [
        DataBlock(
            type="data",
            source=Base64Source(
                type="base64",
                media_type="image/png",
                data=small,
            ),
        ),
    ]
    served = _served_output(_tool_result_msg(output), truncate=True)
    assert small in served


# ---------------------------------------------------------------------------
# 接线：两个历史出口都必须开启
# ---------------------------------------------------------------------------


def test_both_history_paths_enable_truncation() -> None:
    """durable 与 live-window 两条历史出口都要截断。

    只改其中一条会让默认的 Scroll 路径（durable）仍然返回超大响应。
    """
    source = (
        Path(__file__).resolve().parents[4]
        / "src/qwenpaw/app/chats/api.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    calls = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "agentscope_msg_to_message"
        ):
            kwargs = {kw.arg: kw.value for kw in node.keywords}
            enabled = kwargs.get("truncate_oversized_outputs")
            calls.append(
                isinstance(enabled, ast.Constant) and enabled.value is True,
            )

    assert calls, "no agentscope_msg_to_message call found"
    assert all(calls), f"truncation not enabled on every history path: {calls}"
