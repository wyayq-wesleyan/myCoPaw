# -*- coding: utf-8 -*-
"""A name the user chose must never be overwritten by automated naming.

Two independent writers renamed chats without the user asking:

* the backend's LLM title generator (``title_generator``), guarded by an
  atomic compare-and-set against the placeholder name;
* the chat SDK frontend, which renames a session to the raw first message
  whenever its own message array is empty (``useChatController`` ->
  ``updateSessionName``). Opening an existing chat and sending before its
  history hydrated hit that branch, so the name changed "while chatting".

The backend already refused to clobber a *differing* name, but the placeholder
window (`chat.name == name`) let a late or racing generator through, and it had
no notion of "the user set this". ``name_locked`` records that intent: once the
user renames a chat through the API, no automated writer may touch it again.
"""
from __future__ import annotations

import pytest

from qwenpaw.app.chats.manager import ChatManager
from qwenpaw.app.chats.models import ChatSpec, ChatUpdate


class _Repo:
    """Minimal in-memory repo standing in for the JSON repo."""

    def __init__(self):
        self.chats: dict[str, ChatSpec] = {}

    async def get_chat(self, chat_id):
        return self.chats.get(chat_id)

    async def upsert_chat(self, spec):
        self.chats[spec.id] = spec


@pytest.fixture
def mgr(monkeypatch) -> ChatManager:
    manager = ChatManager.__new__(ChatManager)
    import asyncio

    manager._repo = _Repo()
    manager._lock = asyncio.Lock()
    return manager


async def _seed(mgr, name="新建对话") -> ChatSpec:
    spec = ChatSpec(session_id="console:default", user_id="default", name=name)
    await mgr._repo.upsert_chat(spec)
    return spec


async def test_user_rename_locks_the_name(mgr) -> None:
    spec = await _seed(mgr)

    updated = await mgr.patch_chat(
        spec.id, ChatUpdate(name="我的会话"), lock_name=True,
    )

    assert updated is not None
    assert updated.name == "我的会话"
    assert updated.name_locked is True


async def test_generated_title_cannot_overwrite_a_locked_name(mgr) -> None:
    """The compare-and-set used by title generation must refuse."""
    spec = await _seed(mgr, name="第一句用户消息")
    await mgr.patch_chat(spec.id, ChatUpdate(name="用户固定名称"), lock_name=True)

    # Same call the title generator makes, with the placeholder that is now
    # stale. It must not apply.
    result = await mgr.patch_chat_if_name_matches(
        spec.id, "第一句用户消息", ChatUpdate(name="模型生成的标题"),
    )

    assert result is None
    assert (await mgr._repo.get_chat(spec.id)).name == "用户固定名称"


async def test_lock_survives_a_later_placeholder_match(mgr) -> None:
    """Even if the placeholder somehow still matches, the lock wins."""
    spec = await _seed(mgr, name="占位名")
    await mgr.patch_chat(spec.id, ChatUpdate(name="占位名"), lock_name=True)

    assert await mgr.patch_chat_if_name_matches(
        spec.id, "占位名", ChatUpdate(name="自动标题"),
    ) is None
    assert (await mgr._repo.get_chat(spec.id)).name == "占位名"


async def test_background_writes_without_lock_name_do_not_lock(mgr) -> None:
    """Only an explicit user rename locks; internal writes must not."""
    spec = await _seed(mgr)
    # Title generation calls patch_chat_if_name_matches, but any other internal
    # patch must not accidentally freeze the name.
    updated = await mgr.patch_chat(spec.id, ChatUpdate(pinned=True))
    assert updated is not None
    assert updated.name_locked is False


async def test_user_can_rename_again_and_lock_persists(mgr) -> None:
    """The user is always allowed to change their own name."""
    spec = await _seed(mgr)
    await mgr.patch_chat(spec.id, ChatUpdate(name="第一次"), lock_name=True)
    updated = await mgr.patch_chat(spec.id, ChatUpdate(name="第二次"), lock_name=True)

    assert updated is not None and updated.name == "第二次"
    assert updated.name_locked is True


async def test_unlocked_chat_still_gets_an_automatic_title(mgr) -> None:
    """Normal first-message titling must keep working."""
    spec = await _seed(mgr, name="第一句用户消息")

    updated = await mgr.patch_chat_if_name_matches(
        spec.id, "第一句用户消息", ChatUpdate(name="模型生成的标题"),
    )

    assert updated is not None
    assert updated.name == "模型生成的标题"
