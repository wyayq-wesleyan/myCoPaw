# -*- coding: utf-8 -*-
"""删除会话前必须停掉正在跑的 run（#8007）。

原来的删法是：先删 spec，再删 checkpoint；正在跑的 producer 毫不知情，
会继续花模型 token、继续写孤儿会话文件，并让 tracker 里的 running 计数
一直虚高。``request_stop`` 是幂等的（不存在/已完成返回 False），所以对
空闲会话调用它是安全的。

这组用例直接调用端点函数（不经过 FastAPI 依赖注入），只替换
``CHECKPOINT_RUNTIME``、ChatManager 和 task_tracker，重点验证三件事：
1. 删除前确实调了 stop，且顺序在 delete 之前；
2. 空闲会话不受影响；
3. 单个 stop 失败不能阻止删除（逐个容错）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qwenpaw.app.chats import api


class _Tracker:
    def __init__(self, fail_for: set[str] | None = None, running: set[str] | None = None):
        self.calls: list[tuple[str, str]] = []
        self._fail_for = fail_for or set()
        self._running = running or set()

    async def request_stop(self, chat_id: str) -> bool:
        self.calls.append(("stop", chat_id))
        if chat_id in self._fail_for:
            raise RuntimeError("producer crashed")
        return chat_id in self._running


def _chat(chat_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=chat_id,
        session_id=f"session-{chat_id}",
        user_id="default",
        channel="console",
    )


@pytest.fixture()
def patched(monkeypatch):
    """Replace the checkpoint manager so the tests never touch storage."""
    deleted: list[list[tuple[str, str, str]]] = []

    async def _delete_checkpoints(_workspace, keys):
        deleted.append(list(keys))

    monkeypatch.setattr(
        api.CHECKPOINT_RUNTIME,
        "delete_session_checkpoints",
        _delete_checkpoints,
    )
    return SimpleNamespace(deleted=deleted)


def _mgr(chats: list[SimpleNamespace], order: list[tuple[str, object]]):
    mgr = SimpleNamespace()
    mgr.get_chat = AsyncMock(side_effect=lambda chat_id: next(
        (c for c in chats if c.id == chat_id), None,
    ))
    mgr.list_chats = AsyncMock(return_value=chats)

    async def _delete_chats(chat_ids):
        order.append(("delete", list(chat_ids)))
        return True

    mgr.delete_chats = _delete_chats
    return mgr


async def test_delete_chat_stops_the_live_run_first(patched) -> None:
    chat = _chat("c1")
    order: list[tuple[str, object]] = []
    tracker = _Tracker(running={"c1"})

    mgr = _mgr([chat], order)
    workspace = SimpleNamespace(task_tracker=tracker)

    # 让 tracker 的调用也进入同一个顺序表
    original_stop = tracker.request_stop

    async def _stop(chat_id: str) -> bool:
        order.append(("stop", chat_id))
        return await original_stop(chat_id)

    tracker.request_stop = _stop  # type: ignore[method-assign]

    result = await api.delete_chat("c1", mgr=mgr, workspace=workspace)

    assert result == {"deleted": True}
    assert order == [("stop", "c1"), ("delete", ["c1"])]
    assert tracker.calls == [("stop", "c1")]


async def test_delete_idle_chat_is_unaffected(patched) -> None:
    chat = _chat("c2")
    order: list[tuple[str, object]] = []
    tracker = _Tracker()  # 不在 running 集合里

    mgr = _mgr([chat], order)
    result = await api.delete_chat(
        "c2",
        mgr=mgr,
        workspace=SimpleNamespace(task_tracker=tracker),
    )

    assert result == {"deleted": True}
    assert tracker.calls == [("stop", "c2")]
    assert order == [("delete", ["c2"])]


async def test_stop_failure_does_not_block_deletion(patched) -> None:
    chat = _chat("c3")
    order: list[tuple[str, object]] = []
    tracker = _Tracker(fail_for={"c3"})

    mgr = _mgr([chat], order)
    result = await api.delete_chat(
        "c3",
        mgr=mgr,
        workspace=SimpleNamespace(task_tracker=tracker),
    )

    assert result == {"deleted": True}
    assert order == [("delete", ["c3"])]


async def test_batch_delete_stops_every_chat_and_survives_one_failure(
    patched,
) -> None:
    chats = [_chat("a"), _chat("b"), _chat("c")]
    order: list[tuple[str, object]] = []
    tracker = _Tracker(fail_for={"b"})
    mgr = _mgr(chats, order)

    result = await api.batch_delete_chats(
        ["a", "b", "c"],
        mgr=mgr,
        workspace=SimpleNamespace(task_tracker=tracker),
    )

    assert result == {"deleted": True}
    # 三个都尝试停过，且删除发生在所有 stop 之后
    assert tracker.calls == [("stop", "a"), ("stop", "b"), ("stop", "c")]
    assert order == [("delete", ["a", "b", "c"])]
