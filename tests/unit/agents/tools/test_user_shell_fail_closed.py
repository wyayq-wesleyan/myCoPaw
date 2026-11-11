# -*- coding: utf-8 -*-
"""Ordinary-user shell must fail closed when the sandbox cannot start."""

from __future__ import annotations

import pytest

from qwenpaw.app.agent_context import (
    set_current_account_username,
    set_current_agent_id,
    set_current_user_id,
)
from qwenpaw.agents.tools import shell as shell_module
from qwenpaw.sandbox.user_isolation import UserSandboxError


@pytest.mark.asyncio
async def test_user_shell_denies_when_sandbox_unavailable(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "alice"
    root.mkdir()
    set_current_account_username("alice")
    set_current_user_id("alice")
    set_current_agent_id("user_alice_default")

    monkeypatch.setattr(
        "qwenpaw.agents.tools.user_files.account_root",
        lambda *_args, **_kwargs: root,
    )
    monkeypatch.setattr(
        "qwenpaw.app.user_space.is_admin_user",
        lambda _username: False,
    )

    async def _unavailable(*_args, **_kwargs):
        raise UserSandboxError("kernel has no Landlock")

    monkeypatch.setattr(
        "qwenpaw.sandbox.user_isolation.execute_user_command",
        _unavailable,
    )

    host_called = False

    async def _host(*_args, **_kwargs):
        nonlocal host_called
        host_called = True
        raise AssertionError("host shell must not run for an ordinary account")

    monkeypatch.setattr(shell_module, "_execute_posix_host", _host)

    result = await shell_module.execute_shell_command(
        "echo should-not-run",
        cwd=root,
    )

    text = result.content[0].text
    assert "沙箱不可用" in text
    assert result.state.value == "denied"
    assert host_called is False


@pytest.mark.asyncio
async def test_inline_python_is_redirected_to_scripts(tmp_path, monkeypatch):
    root = tmp_path / "alice"
    root.mkdir()
    set_current_account_username("alice")
    set_current_user_id("alice")
    set_current_agent_id("user_alice_default")

    monkeypatch.setattr(
        "qwenpaw.agents.tools.user_files.account_root",
        lambda *_args, **_kwargs: root,
    )
    monkeypatch.setattr(
        "qwenpaw.app.user_space.is_admin_user",
        lambda _username: False,
    )

    async def _must_not_run(*_args, **_kwargs):
        raise AssertionError("inline code must not reach the sandbox")

    monkeypatch.setattr(
        "qwenpaw.sandbox.user_isolation.execute_user_command",
        _must_not_run,
    )

    result = await shell_module.execute_shell_command(
        "python3 - <<'PY'\nprint('inline')\nPY",
        cwd=root,
    )

    text = result.content[0].text
    assert "scripts/<name>.py" in text
    assert "python3 scripts/<name>.py" in text
    assert result.state.value == "success"
