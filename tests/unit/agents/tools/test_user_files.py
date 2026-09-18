"""A write tool may only ever read files inside the calling account's workspace.

Approval is hard-disabled in this deployment, so these containment checks are
the actual gate for "import this file" / "upload this file" tool calls.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from qwenpaw.agents.tools import user_files

EXTS = frozenset({".csv", ".xlsx"})
CSV = "name,amount\n甲,1\n"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Point the resolver at a temporary workspace with a chat media dir."""
    monkeypatch.delenv("CONSOLE_MEDIA_DIR", raising=False)
    monkeypatch.setattr(user_files, "get_current_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(user_files, "resolve_account_username", lambda: "u1")
    media = tmp_path / "media"
    media.mkdir()
    return tmp_path, media


def _write(path: Path, text: str = CSV) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_bare_filename_matches_uuid_prefixed_chat_upload(workspace):
    _, media = workspace
    uploaded = _write(media / "e5f1a2_销售数据.xlsx")

    resolved, error = user_files.resolve_user_file("销售数据.xlsx", EXTS)

    assert error == ""
    assert resolved == uploaded.resolve()


def test_newest_matching_upload_wins(workspace):
    _, media = workspace
    older = _write(media / "aaa_订单.csv")
    newer = _write(media / "bbb_订单.csv")
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    resolved, error = user_files.resolve_user_file("订单.csv", EXTS)

    assert error == ""
    assert resolved == newer.resolve()


def test_absolute_path_inside_workspace_is_allowed(workspace):
    root, _ = workspace
    inside = _write(root / "reports" / "ok.csv")

    resolved, error = user_files.resolve_user_file(str(inside), EXTS)

    assert error == ""
    assert resolved == inside.resolve()


def test_absolute_path_outside_workspace_is_rejected(workspace, tmp_path):
    outside = _write(tmp_path.parent / "someone-else" / "secret.csv")

    resolved, error = user_files.resolve_user_file(str(outside), EXTS)

    assert resolved is None
    assert "不在你的工作区内" in error


def test_symlink_pointing_outside_workspace_is_rejected(workspace, tmp_path):
    """resolve() dereferences the link, so the escape fails containment."""
    root, _ = workspace
    outside = _write(tmp_path.parent / "another-account" / "leak.csv")
    link = root / "innocent.csv"
    link.symlink_to(outside)

    resolved, error = user_files.resolve_user_file(str(link), EXTS)

    assert resolved is None
    assert "不在你的工作区内" in error


def test_traversal_reference_cannot_escape(workspace, tmp_path):
    _write(tmp_path.parent / "up-and-out.csv")

    resolved, error = user_files.resolve_user_file("../../up-and-out.csv", EXTS)

    assert resolved is None
    assert error


def test_relative_subdirectory_reference_is_allowed(workspace):
    root, _ = workspace
    target = _write(root / "reports" / "2024.csv")

    resolved, error = user_files.resolve_user_file("reports/2024.csv", EXTS)

    assert error == ""
    assert resolved == target.resolve()


def test_unsupported_extension_is_rejected(workspace):
    _, media = workspace
    _write(media / "notes.txt", "hello")

    resolved, error = user_files.resolve_user_file("notes.txt", EXTS)

    assert resolved is None
    assert "不支持的文件格式" in error


def test_missing_file_reports_what_is_available(workspace):
    _, media = workspace
    _write(media / "aaa_真实数据.csv")

    resolved, error = user_files.resolve_user_file("不存在.csv", EXTS)

    assert resolved is None
    assert "没有找到" in error
    assert "aaa_真实数据.csv" in error


def test_empty_reference_is_rejected(workspace):
    resolved, error = user_files.resolve_user_file("   ", EXTS)

    assert resolved is None
    assert "请提供" in error


def test_directory_is_not_accepted_as_a_file(workspace):
    root, _ = workspace
    (root / "folder.csv").mkdir()

    resolved, error = user_files.resolve_user_file(str(root / "folder.csv"), EXTS)

    assert resolved is None
    assert error
