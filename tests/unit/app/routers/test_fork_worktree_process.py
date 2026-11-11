# -*- coding: utf-8 -*-
"""``_create_worktree`` 的进程生命周期测试。

这个 helper 由 ``POST /fork/agent`` 调用，替分支会话在工作区里检出独立的
git worktree。两种失败方式都不会在正常路径上出现，必须真起进程才测得到：

1. **检出卡住**：仓库很大或磁盘异常时 git 一直不返回——修复前是 60 秒超时后
   ``proc.kill()``，只杀直接子进程，``git checkout`` 派生的 helper 会留下；
2. **调用方中途离开**：请求被取消时原先没有任何清理，git 继续检出并**持有仓库的
   index.lock**，之后这个仓库上的每一次 git 操作都会被挡住。

用假 ``git`` 起一棵真实进程树来验证，不依赖网络。
"""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
from pathlib import Path
from typing import Iterator

import pytest
from fastapi import HTTPException

from qwenpaw.app.routers import fork as api

HANGING_GIT = """
echo $$ >> "$FAKE_GIT_PID_FILE"
sleep 3600 >/dev/null 2>&1 &
echo $! >> "$FAKE_GIT_PID_FILE"
sleep 3600 >/dev/null 2>&1
"""
# 子进程的输出重定向掉，避免它一直持有管道：否则断言有可能在「子进程自己退出之后」
# 才执行，坏修复也能通过。


@pytest.fixture()
def fake_git(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    """Put a hanging ``git`` first on PATH and return its PID file."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "git"
    script.write_text("#!/bin/sh\n" + HANGING_GIT, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    pid_file = tmp_path / "fakegit.pids"
    monkeypatch.setenv("FAKE_GIT_PID_FILE", str(pid_file))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    yield pid_file


def _pids(path: Path) -> list[int]:
    if not path.exists():
        return []
    return [int(line) for line in path.read_text().split() if line.strip()]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover
        return True
    return True


async def _wait_until(predicate, *, deadline: float = 8.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline
    while loop.time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


async def _wait_for_tree(pid_file: Path) -> list[int]:
    assert await _wait_until(lambda: len(_pids(pid_file)) == 2), (
        f"fake git never got going: {_pids(pid_file)}"
    )
    return _pids(pid_file)


# ---------------------------------------------------------------------------
# 卡住与取消
# ---------------------------------------------------------------------------


async def test_timeout_kills_the_whole_tree(
    tmp_path: Path,
    fake_git: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(api, "_WORKTREE_TIMEOUT_SECONDS", 3.0)
    repo = tmp_path / "proj"
    repo.mkdir()

    task = asyncio.create_task(api._create_worktree(repo, "wt-timeout"))
    # Confirms the tree really existed before the budget ran out, so the
    # "everything is dead" assertion below is about a kill, not about a
    # process that never started.
    pids = await _wait_for_tree(fake_git)
    with pytest.raises(HTTPException) as excinfo:
        await task

    assert "timed out (3s)" in excinfo.value.detail
    assert await _wait_until(lambda: not any(_alive(p) for p in pids)), (
        f"child processes outlived the timeout: {pids}"
    )


async def test_caller_cancellation_kills_the_tree(
    tmp_path: Path,
    fake_git: Path,
) -> None:
    """请求被取消（关页面/断线）时不能把 git 留在后面。

    留着的不只是进程，还有仓库的 index.lock——之后的 git 操作全被挡住。
    """
    repo = tmp_path / "proj"
    repo.mkdir()

    task = asyncio.create_task(api._create_worktree(repo, "wt-cancel"))
    pids = await _wait_for_tree(fake_git)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _wait_until(lambda: not any(_alive(p) for p in pids)), (
        f"worktree checkout survived the cancelled request: {pids}"
    )


# ---------------------------------------------------------------------------
# 正常路径（真 git，本地仓库）
# ---------------------------------------------------------------------------


def _git_available() -> bool:
    import shutil

    return shutil.which("git") is not None


@pytest.mark.skipif(not _git_available(), reason="needs a real git binary")
async def test_creates_a_real_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "-C", str(repo), "add", "-A"],
        ["git", "-C", str(repo), "commit", "-q", "-m", "init"],
    ):
        subprocess.run(cmd, cwd=repo, env=env, check=True, capture_output=True)

    worktree_path, branch = await api._create_worktree(repo, "wt-ok")

    assert branch == "fork/wt-ok"
    assert worktree_path == repo / ".qwenpaw/worktrees/wt-ok"
    assert (worktree_path / "README.md").read_text(encoding="utf-8") == "hello\n"


@pytest.mark.skipif(not _git_available(), reason="needs a real git binary")
async def test_failure_reports_git_stderr(tmp_path: Path) -> None:
    """不是 git 仓库时把 git 的原话带出来，而不是一句无信息的 500。"""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    with pytest.raises(HTTPException) as excinfo:
        await api._create_worktree(not_a_repo, "wt-fail")

    assert excinfo.value.status_code == 500
    assert "git worktree add failed" in excinfo.value.detail
    assert "not a git repository" in excinfo.value.detail.lower()
