# -*- coding: utf-8 -*-
"""Agent tool deletion is preview-first and confined to ``upload/``."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest

from qwenpaw.config.context import set_current_workspace_dir

upload_tool = importlib.import_module("qwenpaw.agents.tools.upload_inbox")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    upload = workspace / "upload"
    (upload / "logs").mkdir(parents=True)
    (upload / "a.txt").write_text("a", encoding="utf-8")
    (upload / "logs" / "old.tmp").write_text("tmp", encoding="utf-8")
    (workspace / "secret.txt").write_text("secret", encoding="utf-8")
    set_current_workspace_dir(str(workspace))
    return workspace


def _run(**kwargs):
    return asyncio.run(upload_tool.delete_upload_files(**kwargs))


def test_dry_run_previews_without_deleting(workspace: Path) -> None:
    result = _run(paths=["upload/a.txt"])

    text = result.content[0].text
    assert "dry_run=true" in text
    assert "upload/a.txt" in text
    assert (workspace / "upload" / "a.txt").is_file()


def test_confirmed_run_deletes_and_reports_freed_bytes(workspace: Path) -> None:
    result = _run(paths=["upload/a.txt"], dry_run=False)

    text = result.content[0].text
    assert "已删除 1 项" in text
    assert "释放" in text
    assert not (workspace / "upload" / "a.txt").exists()


def test_patterns_expand_inside_upload_only(workspace: Path) -> None:
    result = _run(patterns=["**/*.tmp"], dry_run=False)

    assert "已删除 1 项" in result.content[0].text
    assert not (workspace / "upload" / "logs" / "old.tmp").exists()
    assert (workspace / "upload" / "a.txt").is_file()


def test_paths_outside_upload_are_refused(workspace: Path) -> None:
    result = _run(paths=["secret.txt"], dry_run=False)

    assert "Error" in result.content[0].text
    assert (workspace / "secret.txt").is_file()


def test_non_empty_directory_requires_recursive(workspace: Path) -> None:
    refused = _run(paths=["upload/logs"], dry_run=False)
    assert "未删除" in refused.content[0].text
    assert (workspace / "upload" / "logs" / "old.tmp").is_file()

    deleted = _run(paths=["upload/logs"], recursive=True, dry_run=False)
    assert "已删除 1 项" in deleted.content[0].text
    assert not (workspace / "upload" / "logs").exists()


def test_missing_arguments_are_refused_without_side_effects(
    workspace: Path,
) -> None:
    result = _run()

    assert "请提供" in result.content[0].text
    assert (workspace / "upload" / "a.txt").is_file()
