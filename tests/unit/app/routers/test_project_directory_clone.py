# -*- coding: utf-8 -*-
"""``POST /workspace/project-directory/clone`` 的进程与残留管理测试。

这个端点把一条 **SSE 流**包在 ``git clone`` 外面，因此有两类缺陷只有在真起进程、
真关闭生成器时才暴露，mock 掉 subprocess 就完全测不到：

1. **克隆卡住不放手**：网络断了、仓库要凭据、树特别大——进程（连同
   ``git-remote-https`` / ``index-pack`` 这些子进程）会一直活到容器重启。
2. **客户端断开后清理代码不执行**：浏览器关掉页面时，Starlette 是在 ``yield`` 处
   直接关闭生成器的，``yield`` 之后的任何代码都不会跑。所以本文件用
   ``aclose()``（而不是等一个 error 事件）来复现用户真实的中断方式。

断言不依赖鉴权中间件（测试环境不启用），直接调用端点函数并消费返回的
``body_iterator``，与 ``test_paths_api.py`` 同一口径。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

from qwenpaw.app.routers import project_directory as api

REPO_URL = "https://example.invalid/team/demo-repo.git"
REPO_NAME = "demo-repo"


class _Req:
    """Minimal stand-in for a FastAPI Request (identity is mocked)."""


def _install(workspace_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(workspace_dir=workspace_dir, agent_id="agent-clone")


@pytest.fixture()
def workspace_dir(tmp_path: Path) -> Iterator[Path]:
    """A workspace with the coding_projects/ base already in place."""
    (tmp_path / "coding_projects").mkdir(parents=True, exist_ok=True)
    yield tmp_path


def _write_fake_git(bin_dir: Path, body: str) -> None:
    """Install an executable ``git`` that ignores its arguments."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "git"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)


HANGING_GIT = """
echo $$ >> "$FAKE_GIT_PID_FILE"
sleep 3600 >/dev/null 2>&1 &
echo $! >> "$FAKE_GIT_PID_FILE"
echo "Receiving objects: 1% (1/100)"
sleep 3600 >/dev/null 2>&1
"""
# 两个 sleep 都把输出重定向掉：它们若是继续持有那条管道，测试就可能在「子进程自己
# 退出之后」才做断言，于是坏修复也能通过（插件安装那个用例就是这么被骗过一次）。
FAILING_GIT = """
echo "fatal: repository 'x' not found" >&2
exit 1
"""


def _pids(path: Path) -> list[int]:
    if not path.exists():
        return []
    return [int(line) for line in path.read_text().split() if line.strip()]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - other users' processes
        return True
    return True


async def _wait_until(predicate, *, deadline: float = 8.0) -> bool:
    """Poll *predicate* until it holds, so reaping has a chance to happen."""
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline
    while loop.time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


async def _drain(agen, *, timeout: float = 10.0) -> list[str]:
    """Consume an SSE generator completely, with a bounded wait."""

    async def _collect() -> list[str]:
        return [chunk async for chunk in agen]

    return await asyncio.wait_for(_collect(), timeout)


def _events(chunks: list[str]) -> list[str]:
    return [c for c in chunks if c.strip()]


@pytest.fixture()
def patched(monkeypatch, workspace_dir: Path):
    """Wire the endpoint to a temp workspace and a recording config save.

    ``start_command_async`` is wrapped, not replaced: the tests below assert on
    the process it really started, and recording ``owns_process_group`` lets
    them pin the ``start_new_session`` argument the cleanup depends on.
    """
    from qwenpaw.utils import command_runner

    saved: list[tuple[str, str]] = []
    groups: list[bool] = []
    real_start = command_runner.start_command_async

    async def recording_start(*args, **kwargs):
        proc = await real_start(*args, **kwargs)
        groups.append(proc.owns_process_group)
        return proc

    monkeypatch.setattr(
        api,
        "get_agent_for_request",
        lambda request: _awrap(_install(workspace_dir)),
    )
    monkeypatch.setattr(
        api,
        "_save_project_dir",
        lambda agent_id, project_dir: saved.append((agent_id, project_dir)),
    )
    monkeypatch.setattr(api, "start_command_async", recording_start)
    bin_dir = workspace_dir / "fakebin"
    pid_file = workspace_dir / "fakegit.pids"

    def use(body: str) -> Path:
        _write_fake_git(bin_dir, body)
        monkeypatch.setenv("FAKE_GIT_PID_FILE", str(pid_file))
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        return pid_file

    return SimpleNamespace(
        saved=saved,
        groups=groups,
        use=use,
        pid_file=pid_file,
        tmp=workspace_dir,
    )


async def _awrap(value):
    return value


async def _start_clone() -> object:
    body = api.CloneProjectRequest(url=REPO_URL)
    return await api.clone_project(body, _Req())


# ---------------------------------------------------------------------------
# 正常路径（真 git，本地仓库，不碰网络）
# ---------------------------------------------------------------------------


def _git_available() -> bool:
    return shutil.which("git") is not None


def _init_source_repo(path: Path) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "-C", str(path), "add", "-A"],
        ["git", "-C", str(path), "commit", "-q", "-m", "init"],
    ):
        subprocess.run(cmd, cwd=path, env=env, check=True, capture_output=True)


@pytest.mark.skipif(not _git_available(), reason="needs a real git binary")
async def test_local_clone_streams_logs_and_marks_done(patched) -> None:
    source = patched.tmp / "source"
    source.mkdir()
    (source / "README.md").write_text("hello\n", encoding="utf-8")
    _init_source_repo(source)

    response = await api.clone_project(
        api.CloneProjectRequest(url=str(source), name=REPO_NAME),
        _Req(),
    )
    events = _events(await _drain(response.body_iterator))

    assert any('"type": "log"' in e for e in events), events
    assert any('"type": "done"' in e for e in events), events
    assert not any('"type": "error"' in e for e in events), events

    target = patched.tmp / "coding_projects" / REPO_NAME
    assert (target / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert patched.saved == [("agent-clone", str(target))]


async def test_clone_rejects_a_name_that_escapes_the_base(patched) -> None:
    """路径穿越的名字在启动任何进程之前就该被拒。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await api.clone_project(
            api.CloneProjectRequest(url=REPO_URL, name="../evil"),
            _Req(),
        )
    assert excinfo.value.status_code == 400
    assert patched.groups == [], "a rejected clone must not start a process"


# ---------------------------------------------------------------------------
# 卡住的克隆：超时 + 整棵进程树
# ---------------------------------------------------------------------------


async def test_stalled_clone_times_out_and_kills_the_whole_tree(
    patched,
    monkeypatch,
) -> None:
    """卡住的克隆必须在预算内结束，而且子进程不能留下。

    只 kill 直接子进程（``proc.kill()`` 而不是信号整个进程组）时，``sleep`` 会活
    下来占着容器 pids —— 这正是线上「进程数被慢慢吃满」的形态之一。
    """
    pids_file = patched.use(HANGING_GIT)
    monkeypatch.setenv("QWENPAW_GIT_CLONE_TIMEOUT_SECONDS", "2")

    response = await _start_clone()
    agen = response.body_iterator
    # The generator body — and therefore the clone — only runs once the stream
    # is consumed, so the first event is what tells us the tree exists.
    first = await asyncio.wait_for(agen.__anext__(), timeout=10)
    assert '"type": "log"' in first
    assert await _wait_until(lambda: len(_pids(pids_file)) == 2), (
        f"fake git never got going: {_pids(pids_file)}"
    )
    rest = _events(await _drain(agen))
    events = [first, *rest]

    assert any("timed out" in e for e in events), events
    # 守卫:没有 start_new_session 时 owns_process_group 为 False,进程组收不掉
    assert patched.groups == [True], patched.groups
    pids = _pids(pids_file)
    assert await _wait_until(lambda: not any(_alive(p) for p in pids)), (
        f"process group survived the timeout kill: {pids}"
    )
    target = patched.tmp / "coding_projects" / REPO_NAME
    assert await _wait_until(lambda: not target.exists()), (
        "aborted clone left a partial tree behind, which blocks a retry"
    )


async def test_failed_clone_reports_error(patched) -> None:
    patched.use(FAILING_GIT)

    response = await _start_clone()
    events = _events(await _drain(response.body_iterator))

    assert any('"type": "error"' in e and "code 1" in e for e in events), events


# ---------------------------------------------------------------------------
# 客户端断开：yield 之后的清理不会执行，所以清理必须在 finally 里
# ---------------------------------------------------------------------------


async def test_client_disconnect_kills_the_clone(patched) -> None:
    """关掉页面 = Starlette 在 yield 处关闭生成器。

    改动前这里是 ``except (CancelledError, GeneratorExit): pass`` 之后就结束了，
    进程继续跑；``sleep 300`` 之类的克隆会一直挂着。
    """
    pids_file = patched.use(HANGING_GIT)

    response = await _start_clone()
    agen = response.body_iterator
    first = await asyncio.wait_for(agen.__anext__(), timeout=10)
    assert '"type": "log"' in first
    await agen.aclose()  # 用户关页面

    assert patched.groups == [True], patched.groups
    assert await _wait_until(lambda: len(_pids(pids_file)) == 2), (
        f"fake git never got going: {_pids(pids_file)}"
    )
    pids = _pids(pids_file)
    assert await _wait_until(lambda: not any(_alive(p) for p in pids)), (
        f"clone outlived the client that started it: {pids}"
    )
    target = patched.tmp / "coding_projects" / REPO_NAME
    assert await _wait_until(lambda: not target.exists()), (
        "disconnect left a partial clone on disk"
    )


async def test_disconnect_does_not_delete_a_directory_we_did_not_create(
    patched,
) -> None:
    """清理只针对本次创建出来的目录。

    ``git clone`` 对已存在的目录会直接失败；此时若按「没有完成就删掉」处理，就会
    删掉用户自己原有的一整个目录。
    """
    target = patched.tmp / "coding_projects" / REPO_NAME
    target.mkdir(parents=True)
    kept = target / "keep.txt"
    kept.write_text("mine\n", encoding="utf-8")
    patched.use(FAILING_GIT)

    response = await _start_clone()
    events = _events(await _drain(response.body_iterator))
    assert any('"type": "error"' in e for e in events), events

    # 让后台清理任务有机会跑完（它不该删任何东西）
    await asyncio.sleep(0.3)
    assert kept.read_text(encoding="utf-8") == "mine\n"


# ---------------------------------------------------------------------------
# 进度行的切分：git 用 \r 原地刷新，只按 \n 切会看不到进度
# ---------------------------------------------------------------------------


def test_progress_lines_split_on_carriage_return() -> None:
    lines, rest = api._split_progress_lines(
        "Receiving objects: 1%\rReceiving objects: 2%\r\nWriting objects: 3%\n",
    )
    assert lines == [
        "Receiving objects: 1%",
        "Receiving objects: 2%",
        "Writing objects: 3%",
    ]
    assert rest == ""


def test_progress_lines_keep_the_unterminated_tail() -> None:
    lines, rest = api._split_progress_lines("Cloning into 'x'...\nResolving del")
    assert lines == ["Cloning into 'x'..."]
    assert rest == "Resolving del"


def test_unterminated_tail_is_flushed_as_log() -> None:
    """最后一行没有换行符也必须发出去（否则用户看不到结束信息）。"""
    lines, _ = api._split_progress_lines("done but no newline" + "\n")
    assert lines == ["done but no newline"]


# ---------------------------------------------------------------------------
# 超时预算的读取
# ---------------------------------------------------------------------------


def test_clone_timeout_env_parsing(monkeypatch) -> None:
    monkeypatch.delenv("QWENPAW_GIT_CLONE_TIMEOUT_SECONDS", raising=False)
    assert api._clone_timeout_seconds() == api._CLONE_TIMEOUT_DEFAULT

    monkeypatch.setenv("QWENPAW_GIT_CLONE_TIMEOUT_SECONDS", "60")
    assert api._clone_timeout_seconds() == 60.0

    monkeypatch.setenv("QWENPAW_GIT_CLONE_TIMEOUT_SECONDS", " 2.5 ")
    assert api._clone_timeout_seconds() == 2.5

    # 无效值退回默认，而不是变成 0（0 会让每次克隆都立刻超时）
    monkeypatch.setenv("QWENPAW_GIT_CLONE_TIMEOUT_SECONDS", "soon")
    assert api._clone_timeout_seconds() == api._CLONE_TIMEOUT_DEFAULT

    monkeypatch.setenv("QWENPAW_GIT_CLONE_TIMEOUT_SECONDS", "-1")
    assert api._clone_timeout_seconds() == api._CLONE_TIMEOUT_DEFAULT


# ---------------------------------------------------------------------------
# 超时消息的可读性：预算很小时不能显示成 "0s"
# ---------------------------------------------------------------------------


async def test_timeout_message_keeps_fractional_budget(patched, monkeypatch) -> None:
    patched.use(HANGING_GIT)
    monkeypatch.setenv("QWENPAW_GIT_CLONE_TIMEOUT_SECONDS", "0.5")

    response = await _start_clone()
    events = _events(await _drain(response.body_iterator))

    assert any("timed out after 0.5s" in e for e in events), events
