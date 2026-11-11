# -*- coding: utf-8 -*-
"""工具流在中断/断开时不得在 yield 处被关闭（#8212）。

根因（已在本地安装的 AgentScope 2.0.4 上确认）：``Toolkit.call_tool`` 的
末尾是::

    finally:
        yield tool_response

所以在「INTERRUPTED chunk 的 yield 处」调用 ``aclose()``，GeneratorExit 会
进入那个 ``finally``，而 finally 又 yield → ``RuntimeError: async generator
ignored GeneratorExit``。

修复方式是**继续消费到 EOF**，而不是提前关闭。本文件用假生成器复现根因，
并验证两处真实调用点（``run_tool_batch._call_tool``、``ToolCoordinator._drain``）
都改成了 drain，且 drain 有上限。
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncGenerator

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk, ToolResponse

import importlib

rtb = importlib.import_module("qwenpaw.agents.tools.run_tool_batch")
from qwenpaw.tool_calls._coordinator import (
    _MAX_POST_TERMINAL_CHUNKS,
    ToolCoordinator,
)

_ROOT = Path(__file__).resolve().parents[4]


def _interrupted(text: str = "interrupted") -> ToolChunk:
    return ToolChunk(
        content=[TextBlock(type="text", text=text)],
        state=ToolResultState.INTERRUPTED,
    )


# ---------------------------------------------------------------------------
# 根因复现（不依赖被测代码）
# ---------------------------------------------------------------------------


async def test_closing_at_interrupted_yield_raises() -> None:
    """这就是上游描述的失败：在 finally-yield 的生成器上 aclose()。"""

    async def agentscope_like() -> AsyncGenerator[Any, None]:
        try:
            yield _interrupted()
        finally:
            yield ToolResponse(id="c", content=[TextBlock(type="text", text="final")])

    gen = agentscope_like()
    await gen.__anext__()
    with pytest.raises(RuntimeError, match="ignored GeneratorExit"):
        await gen.aclose()


# ---------------------------------------------------------------------------
# run_tool_batch._call_tool
# ---------------------------------------------------------------------------


def _patch_toolkit(monkeypatch, call_tool) -> None:
    toolkit = SimpleNamespace(call_tool=call_tool)
    monkeypatch.setattr(rtb, "get_current_toolkit", lambda: toolkit)
    monkeypatch.setattr(rtb, "get_current_agent_state", lambda: object())


async def test_call_tool_drains_after_interruption(monkeypatch) -> None:
    """修复前这里会 break，留下一个待回收的生成器；修复后必须消费到 EOF。"""
    state = {"completed": False}

    async def stream() -> AsyncGenerator[Any, None]:
        try:
            yield _interrupted()
        finally:
            yield ToolResponse(id="c", content=[TextBlock(type="text", text="final")])
        state["completed"] = True

    _patch_toolkit(monkeypatch, lambda tool_call, agent_state: stream())

    response = await rtb._call_tool("demo", {})

    assert state["completed"] is True, "generator was abandoned at the yield"
    assert response.state == ToolResultState.INTERRUPTED


async def test_call_tool_caps_post_terminal_drain(monkeypatch) -> None:
    """中断后仍无限产出的工具不能把整轮拖住。"""
    state = {"consumed": 0}

    async def stream() -> AsyncGenerator[Any, None]:
        yield _interrupted()
        for i in range(1000):
            state["consumed"] += 1
            yield ToolChunk(
                content=[TextBlock(type="text", text=f"noise-{i}")],
            )

    _patch_toolkit(monkeypatch, lambda tool_call, agent_state: stream())

    response = await rtb._call_tool("demo", {})

    assert response.state == ToolResultState.INTERRUPTED
    assert state["consumed"] <= _MAX_POST_TERMINAL_CHUNKS + 1


async def test_call_tool_cleanup_error_does_not_mask_interruption(
    monkeypatch,
) -> None:
    """中断之后的清理异常不能被替换成一条普通错误（会丢掉中断语义）。"""

    async def stream() -> AsyncGenerator[Any, None]:
        try:
            yield _interrupted()
        finally:
            raise RuntimeError("cleanup exploded")

    _patch_toolkit(monkeypatch, lambda tool_call, agent_state: stream())

    response = await rtb._call_tool("demo", {})

    assert response.state == ToolResultState.INTERRUPTED


# ---------------------------------------------------------------------------
# ToolCoordinator._drain
# ---------------------------------------------------------------------------


class _Stream:
    def __init__(self) -> None:
        self.items: list[Any] = []
        self.closed = 0

    async def append(self, item: Any) -> None:
        self.items.append(item)

    async def close(self) -> None:
        self.closed += 1


def _entry() -> SimpleNamespace:
    return SimpleNamespace(
        final_response=ToolResponse(id="call-1", content=[]),
        stream=_Stream(),
        end_state=None,
        ctx=SimpleNamespace(tool_name="demo", tool_call_id="call-1"),
    )


async def test_coordinator_drain_consumes_the_final_response() -> None:
    state = {"completed": False}

    def next_handler(*, tool_call: Any):
        async def gen():
            try:
                yield _interrupted()
            finally:
                yield ToolResponse(
                    id="call-1",
                    content=[TextBlock(type="text", text="final")],
                )
            state["completed"] = True

        return gen()

    entry = _entry()
    await ToolCoordinator._drain(
        SimpleNamespace(),
        next_handler,
        tool_call=SimpleNamespace(),
        entry=entry,
    )

    assert state["completed"] is True
    assert entry.end_state == "interrupted"
    assert entry.stream.closed == 1
    assert any(
        getattr(item, "state", None) == ToolResultState.INTERRUPTED
        for item in entry.stream.items
    )


async def test_coordinator_drain_is_capped() -> None:
    state = {"consumed": 0}

    def next_handler(*, tool_call: Any):
        async def gen():
            yield _interrupted()
            for i in range(1000):
                state["consumed"] += 1
                yield ToolChunk(
                    content=[TextBlock(type="text", text=f"noise-{i}")],
                )

        return gen()

    entry = _entry()
    await ToolCoordinator._drain(
        SimpleNamespace(),
        next_handler,
        tool_call=SimpleNamespace(),
        entry=entry,
    )

    assert entry.end_state == "interrupted"
    assert state["consumed"] <= _MAX_POST_TERMINAL_CHUNKS + 1


# ---------------------------------------------------------------------------
# Runtime：GeneratorExit 分支不能 yield
# ---------------------------------------------------------------------------


def test_runtime_generator_exit_handler_is_yield_free() -> None:
    """``Runtime.run`` 是异步生成器；在 GeneratorExit 分支里 yield 会触发
    同一个 RuntimeError。这里做结构性守卫（行为面由上面的 drain 用例覆盖）。
    """
    source = (_ROOT / "src/qwenpaw/runtime/runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    handlers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Name)
        and node.type.id == "GeneratorExit"
    ]
    assert len(handlers) == 1, "Runtime.run 必须显式处理 GeneratorExit"

    forbidden = [
        node
        for node in ast.walk(handlers[0])
        if isinstance(node, (ast.Yield, ast.YieldFrom))
    ]
    assert forbidden == [], "GeneratorExit 分支内不允许 yield"
