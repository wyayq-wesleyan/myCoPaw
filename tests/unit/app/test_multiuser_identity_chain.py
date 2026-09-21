"""Account identity must survive every entry path, not just console chat.

The system carries two different notions of "user" and never distinguished them:

* the **session** user id — ``"default"`` for console, and for scheduled runs the
  channel dispatch target (``"cron"``, ``"main"``, an IM sender id). It is also
  written into sub-agent/fork payloads where it has to keep matching the stored
  session.
* the **owning account** — what account-scoped tools must use as the workspace
  root.

Account tools read the session field, so a cron/heartbeat run resolved an account
named ``"main"``/``"cron"``, i.e. a workspace that does not exist: its file tools
were refused and its token usage was booked against a fake account. Measured on
a real config, ``resolve_account_username()`` returned ``"main"``.

These tests pin the separation: a dedicated channel carries the account, the
request hook populates it, and the account tools prefer it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def account_channel():
    """Reset the account ContextVar around each test."""
    from qwenpaw.app.agent_context import set_current_account_username

    set_current_account_username(None)
    yield set_current_account_username
    set_current_account_username(None)


# --------------------------------------------------------------------------
# The dedicated channel exists and is separate from the session user id
# --------------------------------------------------------------------------


def test_account_channel_is_separate_from_the_session_user_id(account_channel):
    from qwenpaw.app.agent_context import (
        get_current_account_username,
        get_current_user_id,
        set_current_user_id,
    )

    set_current_user_id("main")  # heartbeat's session field
    assert get_current_user_id() == "main"

    account_channel("bob")
    assert get_current_account_username() == "bob"
    # Setting one must not disturb the other: fork/sub-agent payloads rely on
    # the session value still being readable.
    assert get_current_user_id() == "main"


# --------------------------------------------------------------------------
# Account tools prefer the account channel
# --------------------------------------------------------------------------


def test_tool_account_resolution_prefers_the_account_channel(
    account_channel,
    monkeypatch,
):
    from qwenpaw.app.agent_context import set_current_user_id
    from qwenpaw.agents.tools import user_files

    set_current_user_id("dingtalk_zhang")  # a dispatch target, not an account
    account_channel("bob")

    assert user_files.resolve_account_username() == "bob"


def test_tool_account_resolution_still_uses_the_session_id_when_no_account(
    account_channel,
    monkeypatch,
):
    """Interactive paths that never set the account channel keep working."""
    from qwenpaw.app.agent_context import set_current_user_id
    from qwenpaw.agents.tools import user_files

    set_current_user_id("alice")

    assert user_files.resolve_account_username() == "alice"


def test_usage_attribution_prefers_the_account_channel(account_channel):
    from qwenpaw.app.agent_context import set_current_user_id
    from qwenpaw.token_usage.model_wrapper import _resolve_usage_user_id

    set_current_user_id("main")
    account_channel("bob")

    assert _resolve_usage_user_id() == "bob"


# --------------------------------------------------------------------------
# The request hook populates the channel, falling back to the agent's owner
# --------------------------------------------------------------------------


def _config_with_owner(tmp_path: Path, agent_id: str, owner: str):
    from qwenpaw.config import Config
    from qwenpaw.config.config import AgentProfileRef

    config = Config()
    config.agents.profiles[agent_id] = AgentProfileRef(
        id=agent_id,
        workspace_dir=str(tmp_path / "ws"),
        owner_username=owner,
    )
    return config


@pytest.mark.asyncio
async def test_hook_resolves_the_account_from_the_agent_owner(
    tmp_path,
    monkeypatch,
    account_channel,
):
    """cron/heartbeat carry no account, so the owner must supply it."""
    from qwenpaw.app.agent_context import get_current_account_username
    from qwenpaw.hooks.request_setup.contextvars_hook import ContextVarsSetupHook
    from qwenpaw.runtime.hooks import HookContext
    import qwenpaw.app.user_space as user_space
    import qwenpaw.config.config as config_impl

    # ``owner_for_agent`` resolves through user_space's own import of
    # ``load_config``, so patch it there.
    monkeypatch.setattr(
        user_space,
        "load_config",
        lambda: _config_with_owner(tmp_path, "user_bob_default", "bob"),
    )
    # The hook separately reads agent settings for shell/project defaults; that
    # lookup is optional and already tolerated, so leave it failing softly
    # rather than build a full agent config.
    def _no_agent_config(*_args, **_kwargs):
        raise RuntimeError("not needed for this test")

    monkeypatch.setattr(config_impl, "load_agent_config", _no_agent_config)

    request = SimpleNamespace(
        user_id="main",  # heartbeat's session field, not an account
        channel="console",
        request_context={"source": "heartbeat"},
    )
    ctx = HookContext(
        request=request,
        session_id="main",
        agent_id="user_bob_default",
        root_session_id="main",
        root_agent_id="user_bob_default",
        workspace_dir=tmp_path / "ws",
        workspace=None,
        app_services=None,
        input_msgs=None,
    )

    await ContextVarsSetupHook().run(ctx)

    assert get_current_account_username() == "bob"


@pytest.mark.asyncio
async def test_hook_prefers_an_explicitly_injected_account(
    tmp_path,
    monkeypatch,
    account_channel,
):
    """The server-provided identity wins over the owner lookup."""
    from qwenpaw.app.agent_context import get_current_account_username
    from qwenpaw.hooks.request_setup.contextvars_hook import ContextVarsSetupHook
    from qwenpaw.runtime.hooks import HookContext

    request = SimpleNamespace(
        user_id="default",
        channel="console",
        request_context={"account_username": "alice"},
    )
    ctx = HookContext(
        request=request,
        session_id="s1",
        agent_id="user_bob_default",
        root_session_id="s1",
        root_agent_id="user_bob_default",
        workspace_dir=tmp_path / "ws",
        workspace=None,
        app_services=None,
        input_msgs=None,
    )

    await ContextVarsSetupHook().run(ctx)

    assert get_current_account_username() == "alice"


# --------------------------------------------------------------------------
# ReMe recipient resolution must not parse the (hashed) agent id
# --------------------------------------------------------------------------


def test_reme_recipient_uses_the_owner_not_the_agent_id(tmp_path, monkeypatch):
    """``_slug`` hashes non-ASCII names, so splitting the id yielded "u"."""
    import qwenpaw.app.user_space as user_space

    monkeypatch.setattr(user_space, "owner_for_agent", lambda _id: "张伟")

    assert user_space.owner_for_agent("user_u_abc_default") == "张伟"
