"""Phase-2: file and shell tools are confined to the caller's account.

In this deployment the governance policy engine never runs (approval is
hard-disabled) and the sandbox is off, so the only thing that can keep one
account out of another's files is a check inside the tool itself. These tests
pin that check for every file-touching tool, including that a refusal is a
readable tool result rather than an unhandled exception.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from qwenpaw.agents.tools import user_files
from qwenpaw.app.agent_context import set_current_user_id
from qwenpaw.app.user_space import get_user_root
from qwenpaw.config.context import (
    set_current_project_dir,
    set_current_workspace_dir,
)

file_io = importlib.import_module("qwenpaw.agents.tools.file_io")
file_search = importlib.import_module("qwenpaw.agents.tools.file_search")
send_file = importlib.import_module("qwenpaw.agents.tools.send_file")
view_media = importlib.import_module("qwenpaw.agents.tools.view_media")
shell = importlib.import_module("qwenpaw.agents.tools.shell")
run_tool_batch = importlib.import_module("qwenpaw.agents.tools.run_tool_batch")
desktop_screenshot = importlib.import_module(
    "qwenpaw.agents.tools.desktop_screenshot",
)

# This module tests the confinement itself, so it must not use the directory
# conftest fixture that widens the account root for behaviour-only tests.
pytestmark = pytest.mark.real_account_scope


@pytest.fixture
def two_accounts(tmp_path, monkeypatch):
    """Two accounts with one file each, acting as alice."""
    monkeypatch.setenv("QWENPAW_WORKING_DIR", str(tmp_path))
    monkeypatch.delenv("CONSOLE_MEDIA_DIR", raising=False)

    alice_root = get_user_root("alice")
    bob_root = get_user_root("bob")
    alice_root.mkdir(parents=True, exist_ok=True)
    bob_root.mkdir(parents=True, exist_ok=True)

    (alice_root / "mine.txt").write_text("alice own", encoding="utf-8")
    (bob_root / "secret.txt").write_text("bob private", encoding="utf-8")
    (bob_root / "plan.json").write_text("[]", encoding="utf-8")

    set_current_workspace_dir(str(alice_root))
    set_current_project_dir(str(alice_root))
    set_current_user_id("alice")

    return alice_root, bob_root


def _text(result) -> str:
    block = result.content[0]
    return block.text if hasattr(block, "text") else str(block)


# --------------------------------------------------------------- shared gate


def test_ensure_inside_account_allows_the_own_root(two_accounts):
    alice_root, _ = two_accounts
    assert user_files.ensure_inside_account(alice_root / "mine.txt")


def test_ensure_inside_account_rejects_another_account(two_accounts):
    _, bob_root = two_accounts
    with pytest.raises(user_files.AccountScopeError):
        user_files.ensure_inside_account(bob_root / "secret.txt")


def test_ensure_inside_account_rejects_a_symlink_escape(two_accounts, tmp_path):
    """resolve() runs first, so a link out of the account is caught."""
    alice_root, bob_root = two_accounts
    link = alice_root / "innocent.txt"
    link.symlink_to(bob_root / "secret.txt")

    with pytest.raises(user_files.AccountScopeError):
        user_files.ensure_inside_account(link)


# ------------------------------------------------------------------ file I/O


@pytest.mark.asyncio
async def test_read_file_refuses_another_account(two_accounts):
    _, bob_root = two_accounts
    text = _text(await file_io.read_file(str(bob_root / "secret.txt")))

    assert "bob private" not in text
    assert "只能访问自己账号工作区内的文件" in text


@pytest.mark.asyncio
async def test_read_file_still_reads_your_own(two_accounts):
    alice_root, _ = two_accounts
    assert "alice own" in _text(await file_io.read_file(str(alice_root / "mine.txt")))


@pytest.mark.asyncio
async def test_write_file_refuses_another_account(two_accounts):
    _, bob_root = two_accounts
    target = bob_root / "written.txt"
    _text(await file_io.write_file(str(target), "intrusion"))

    assert not target.exists(), "another account's directory was written to"


@pytest.mark.asyncio
async def test_edit_and_append_refuse_another_account(two_accounts):
    _, bob_root = two_accounts
    before = (bob_root / "secret.txt").read_text(encoding="utf-8")

    await file_io.edit_file(str(bob_root / "secret.txt"), "bob private", "changed")
    await file_io.append_file(str(bob_root / "secret.txt"), "extra")

    assert (bob_root / "secret.txt").read_text(encoding="utf-8") == before


# --------------------------------------------------------------- search tools


def test_grep_and_glob_refuse_another_account(two_accounts):
    _, bob_root = two_accounts

    grep_root = file_search._resolve_search_root(str(bob_root))
    glob_root = file_search._resolve_search_root(str(bob_root), require_dir=True)

    # Both return an error ToolChunk rather than raising.
    assert "只能访问自己账号工作区内的文件" in _text(grep_root)
    assert "只能访问自己账号工作区内的文件" in _text(glob_root)


def test_grep_still_accepts_your_own_root(two_accounts):
    alice_root, _ = two_accounts
    assert isinstance(file_search._resolve_search_root(str(alice_root)), type(alice_root))


# ------------------------------------------------------- send/media tools


@pytest.mark.asyncio
async def test_send_file_refuses_another_account(two_accounts):
    _, bob_root = two_accounts
    text = _text(await send_file.send_file_to_user(str(bob_root / "secret.txt")))

    assert "只能访问自己账号工作区内的文件" in text


@pytest.mark.asyncio
async def test_view_image_refuses_another_account(two_accounts):
    _, bob_root = two_accounts
    (bob_root / "pic.png").write_bytes(b"\x89PNG\r\n")

    resolved, error = view_media._validate_media_path(
        str(bob_root / "pic.png"),
        {".png"},
        "image",
    )

    assert error is not None
    assert "只能访问自己账号工作区内的文件" in _text(error)


# ---------------------------------------------------------------------- shell


@pytest.mark.asyncio
async def test_shell_refuses_a_command_naming_another_account(two_accounts):
    _, bob_root = two_accounts
    text = _text(
        await shell.execute_shell_command(f"cat {bob_root / 'secret.txt'}"),
    )

    assert "bob private" not in text
    assert "其他账号的路径" in text


@pytest.mark.asyncio
async def test_shell_refuses_a_cwd_outside_the_account(two_accounts):
    _, bob_root = two_accounts
    text = _text(await shell.execute_shell_command("ls", cwd=str(bob_root)))

    assert "只能访问自己账号工作区内的文件" in text


@pytest.mark.asyncio
async def test_shell_still_runs_normal_commands(two_accounts):
    """The guard must not break ordinary use."""
    text = _text(await shell.execute_shell_command("echo hi && ls"))

    assert "hi" in text
    assert "mine.txt" in text


@pytest.mark.asyncio
async def test_shell_allows_referencing_your_own_path(two_accounts):
    alice_root, _ = two_accounts
    text = _text(await shell.execute_shell_command(
        f"cat {alice_root / 'mine.txt'}",
    ))

    assert "alice own" in text


# ------------------------------------------------------------ batch / capture


def test_batch_file_refuses_another_account(two_accounts):
    _, bob_root = two_accounts
    with pytest.raises(ValueError, match="只能访问自己账号工作区内的文件"):
        run_tool_batch._load_batch_file(str(bob_root / "plan.json"))


def test_batch_file_accepts_your_own(two_accounts):
    alice_root, _ = two_accounts
    (alice_root / "plan.json").write_text("[]", encoding="utf-8")

    assert run_tool_batch._load_batch_file(str(alice_root / "plan.json")) == []


def test_screenshot_refuses_another_account(two_accounts, monkeypatch):
    _, bob_root = two_accounts
    target = bob_root / "shot.png"

    # Stop before the actual OS capture: the refusal must happen first.
    def _unexpected(*_a, **_k):
        raise AssertionError("capture ran despite an out-of-scope path")

    monkeypatch.setattr(
        desktop_screenshot,
        "_capture_macos_screencapture",
        _unexpected,
    )
    monkeypatch.setattr(
        desktop_screenshot,
        "_capture_windows",
        _unexpected,
        raising=False,
    )

    result = asyncio.run(
        desktop_screenshot.desktop_screenshot(path=str(target)),
    )

    # The tool answers with JSON, so decode it instead of matching the raw
    # (escaped) text.
    import json as _json

    payload = _json.loads(result)
    assert payload["ok"] is False
    assert "只能访问自己账号工作区内的文件" in payload["error"]
