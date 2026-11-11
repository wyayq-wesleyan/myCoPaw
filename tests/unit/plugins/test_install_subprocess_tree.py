# -*- coding: utf-8 -*-
"""插件依赖安装的子进程生命周期。

``_run_subprocess_with_streaming_log`` 装的是 pip/uv 依赖——它们会再派生编译器与
构建后端。超时时只 kill 直接子进程的话，那棵子树会继续跑：既占着容器的 pids，
也继续吃 CPU。与知识服务解析、git 调用是同一类缺陷，因此用同一个口径验证：
真起一棵进程树，再确认整棵都死了。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Iterator

import pytest

from qwenpaw.plugins.loader import PluginLoader

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")


@pytest.fixture()
def pid_file(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    path = tmp_path / "tree.pids"
    monkeypatch.setenv("PID_FILE", str(path))
    yield path


HANGING_CMD = [
    "/bin/sh",
    "-c",
    # Both sleeps send their output to /dev/null on purpose. The helper reads
    # the merged pipe in a thread, so a leftover child still holding that pipe
    # keeps the *caller* waiting until it exits — which would give the bug time
    # to disappear on its own before the assertion runs. (This test originally
    # used `sleep 300` and reported a pass after 300 seconds of waiting, with
    # the child already gone by the time it checked.)
    'echo $$ >> "$PID_FILE"; '
    'sleep 3600 >/dev/null 2>&1 & echo $! >> "$PID_FILE"; '
    "sleep 3600 >/dev/null 2>&1",
]


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


def _wait_dead(pids: list[int], *, deadline: float = 5.0) -> bool:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if not any(_alive(p) for p in pids):
            return True
        time.sleep(0.05)
    return not any(_alive(p) for p in pids)


def test_timeout_kills_the_whole_install_tree(pid_file: Path) -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        PluginLoader._run_subprocess_with_streaming_log(
            HANGING_CMD,
            timeout=1,
            plugin_id="demo",
        )

    pids = _pids(pid_file)
    assert len(pids) == 2, f"expected the installer and its child: {pids}"
    assert _wait_dead(pids), f"install tree outlived the timeout: {pids}"


def test_output_is_captured_from_both_streams() -> None:
    """stderr 与 stdout 合并采集（改动只碰清理，不该影响这段）。"""
    result = PluginLoader._run_subprocess_with_streaming_log(
        ["/bin/sh", "-c", "echo out-line; echo err-line >&2"],
        timeout=30,
        plugin_id="demo",
    )

    assert result.returncode == 0
    assert "out-line" in result.stdout
    assert "err-line" in result.stdout
