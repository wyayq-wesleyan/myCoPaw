# -*- coding: utf-8 -*-
"""Regression tests for live chat session checkpoints."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qwenpaw.runtime.runtime import Runtime


def _ctx(*, ephemeral: bool = False):
    request_context = {"qwenpaw.ephemeral": True} if ephemeral else {}
    session = SimpleNamespace(save_session_state=AsyncMock())
    agent = SimpleNamespace(state_dict=lambda: {"state": {"context": []}})
    request = SimpleNamespace(user_id="用户甲", channel="console", request_context=request_context)
    workspace = SimpleNamespace(session=session)
    ctx = SimpleNamespace(
        request=request,
        session_id="session-1",
        workspace=workspace,
        agent=agent,
        mode_state={"mission": {"active": False}},
    )
    return ctx, session


@pytest.mark.asyncio
async def test_session_checkpoint_uses_request_identity_and_mode_state():
    ctx, session = _ctx()

    await Runtime._save_session_checkpoint(None, ctx)

    session.save_session_state.assert_awaited_once()
    kwargs = session.save_session_state.await_args.kwargs
    assert kwargs["session_id"] == "session-1"
    assert kwargs["user_id"] == "用户甲"
    assert kwargs["channel"] == "console"
    assert kwargs["agent"].data["mode_state"] == ctx.mode_state


@pytest.mark.asyncio
async def test_session_checkpoint_skips_ephemeral_request():
    ctx, session = _ctx(ephemeral=True)

    await Runtime._save_session_checkpoint(None, ctx)

    session.save_session_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_checkpoint_save_failure_does_not_raise():
    ctx, session = _ctx()
    session.save_session_state.side_effect = OSError("disk unavailable")

    await Runtime._save_session_checkpoint(None, ctx)
