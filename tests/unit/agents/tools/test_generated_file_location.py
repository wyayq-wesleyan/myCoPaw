"""Generated files must land where the console can serve them.

A file written outside the requesting account's workspace is refused at
preview time (HTTP 403 with the generic "no permission" message), because the
preview endpoint confines non-administrators to their own account root. DOCX
and chart generation used to resolve their output directory from the
``QWENPAW_WORKSPACE_DIR`` environment variable, which no deployment sets, so
every generated file went to the global ``WORKING_DIR/media`` and no normal
account could open it.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from qwenpaw.agents.tools import user_files

docx_tool = importlib.import_module("qwenpaw.agents.tools.generate_docx")
chart_tool = importlib.import_module("qwenpaw.agents.tools.generate_chart")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Point the request context at a temporary agent workspace."""
    monkeypatch.delenv("QWENPAW_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("CONSOLE_MEDIA_DIR", raising=False)
    monkeypatch.setattr(
        user_files,
        "get_current_workspace_dir",
        lambda: tmp_path,
    )
    return tmp_path


def test_docx_uses_the_request_workspace_media_dir(workspace):
    """The output must sit under the agent workspace, not the global dir."""
    resolved = docx_tool._resolve_media_dir()

    assert resolved == workspace / "media"
    assert resolved.is_dir()


def test_chart_uses_the_request_workspace_media_dir(workspace):
    resolved = chart_tool._resolve_media_dir()

    assert resolved == workspace / "media"
    assert resolved.is_dir()


def test_media_dir_matches_the_console_channel_resolution(workspace):
    """Both tools must agree with the directory chat uploads are stored in."""
    assert docx_tool._resolve_media_dir() == chart_tool._resolve_media_dir()


def test_runtime_env_var_no_longer_redirects_the_output(tmp_path, monkeypatch):
    """The unset-everywhere env var must have no effect any more."""
    monkeypatch.setenv("QWENPAW_WORKSPACE_DIR", str(tmp_path / "elsewhere"))
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(
        user_files,
        "get_current_workspace_dir",
        lambda: workspace,
    )

    assert docx_tool._resolve_media_dir() == workspace / "media"
    assert not (tmp_path / "elsewhere" / "media").exists()


def test_falls_back_to_the_global_media_dir_without_a_context(monkeypatch):
    """Outside a request (e.g. a bare run) the previous default still applies."""
    monkeypatch.setattr(
        user_files,
        "get_current_workspace_dir",
        lambda: None,
    )
    from qwenpaw.app.channels.console.channel import DEFAULT_MEDIA_DIR

    assert docx_tool._resolve_media_dir() == DEFAULT_MEDIA_DIR


def test_console_media_override_is_honoured(tmp_path, monkeypatch):
    """An explicit CONSOLE_MEDIA_DIR wins, matching the channel's own rule."""
    monkeypatch.setenv("CONSOLE_MEDIA_DIR", str(tmp_path / "shared"))

    assert docx_tool._resolve_media_dir() == tmp_path / "shared"


def test_generated_path_is_inside_the_account_workspace(workspace):
    """The preview endpoint's containment rule is what rejected the file."""
    media_dir = docx_tool._resolve_media_dir()
    produced = media_dir / "abc123_报告.docx"

    # This is the same check preview_file applies to a non-administrator.
    assert produced.resolve().is_relative_to(workspace.resolve())


@pytest.mark.asyncio
async def test_resolution_holds_across_the_real_request_hook(tmp_path, monkeypatch):
    """The contextvar must survive the real request-setup hook.

    ContextVars are per-task: a hook awaited in one async context is invisible
    to code running after an ``asyncio.run`` boundary, so this exercises the
    hook and the tool inside one coroutine — the way a real request runs. An
    earlier check done across that boundary wrongly suggested the fix did not
    work.
    """
    monkeypatch.setenv("QWENPAW_WORKING_DIR", str(tmp_path))
    monkeypatch.delenv("CONSOLE_MEDIA_DIR", raising=False)

    from qwenpaw.hooks.request_setup.contextvars_hook import ContextVarsSetupHook
    from qwenpaw.runtime.hooks import HookContext

    agent_ws = tmp_path / "workspaces" / "u_abc"
    agent_ws.mkdir(parents=True, exist_ok=True)

    class _Request:
        user_id = "zhang"
        channel = "console"
        request_context = {"account_username": "zhang"}

    ctx = HookContext(
        request=_Request(),
        session_id="s1",
        agent_id="default",
        root_session_id="s1",
        root_agent_id="default",
        workspace_dir=agent_ws,
        workspace=None,
        app_services=None,
        input_msgs=None,
    )

    await ContextVarsSetupHook().run(ctx)

    resolved = docx_tool._resolve_media_dir()
    assert resolved == agent_ws / "media"
    # The shared global directory is what the preview endpoint rejects.
    assert resolved != tmp_path / "media"
