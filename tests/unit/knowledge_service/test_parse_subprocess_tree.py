# -*- coding: utf-8 -*-
"""解析子进程必须整棵树一起收掉，不能只杀直接子进程。

线上故障（2026-10-17）：一次 DOCX 转换报 `soffice: 43: Cannot fork`，紧接着三分钟内
`GET /v1/documents` 连续 17 次 500，错误是 `RuntimeError: can't start new thread`。

成因是超时处理只杀了直接子进程：
- `soffice` 是个 shell 包装脚本，真正干活的是它 exec 出来的 `soffice.bin`，后者还会
  自己 fork；杀掉包装进程不等于杀掉它们。
- MinerU 走 `shell=True`，直接子进程只是 `/bin/sh`，真正的 python 进程在它下面。

于是一次卡住的转换会留下整棵残留进程树，"卡住的转换"多几次就把容器的 fork 能力
（pids 配额）耗尽 —— 此后所有 `fork` 失败（`Cannot fork`），`asyncio.to_thread`
也创不了线程（`can't start new thread`），共享同一进程的文档列表端点跟着全挂。

修法：`start_new_session=True` 让子进程自成一个进程组，超时时对整个组发 SIGKILL。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PARSERS_PATH = REPO_ROOT / "knowledge-service" / "app" / "parsers.py"


def _parsers_module():
    """Load parsers.py without importing the whole knowledge-service package."""
    spec = importlib.util.spec_from_file_location("kb_parsers_under_test", PARSERS_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pid_alive(pid: int) -> bool:
    """Whether *pid* is a live (non-zombie) process.

    ``os.kill(pid, 0)`` answers this on every POSIX system. The extra
    ``/proc`` read only exists to reject zombies, and it must not run where
    ``/proc`` is absent: on macOS that read raises, and treating the error as
    "dead" made this helper report every live process as gone — which is how
    the first version of this test passed against a deliberately broken fix.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    proc_stat = Path(f"/proc/{pid}/stat")
    if not proc_stat.exists():
        return True
    try:
        state = proc_stat.read_text(encoding="utf-8").split(") ", 1)[1].split(" ", 1)[0]
    except (OSError, IndexError):
        return True
    return state != "Z"


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
def test_timeout_kills_the_whole_process_tree(tmp_path: Path) -> None:
    """父进程下的孙进程也必须被杀死。

    脚本：父进程再起一个子进程，两者都 sleep 很久；把孙进程 pid 写进文件。
    超时后断言孙进程已死 —— 只杀直接子进程时它会活下来。
    """
    parsers = _parsers_module()
    pid_file = tmp_path / "grandchild.pid"

    # 关键：孙进程必须是「父进程被杀后自己不会跟着退」的形态。
    #
    # `sh -c 'sleep 300 & wait'` 不行：sh 被 SIGKILL 后，它的非交互式 shell
    # 退出会让后台作业一起收到 SIGHUP，sleep 跟着结束 —— 那样即使只杀直接子
    # 进程，用例也会通过（假阳性）。这里用一个 Python 父进程 fork 出一个
    # **ignore SIGHUP** 的孙进程，模拟 soffice.bin / MinerU worker 那种
    # 真正会残留下来的进程。
    launcher = tmp_path / "launch_tree.py"
    launcher.write_text(
        "import os, signal, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "    time.sleep(300)\n"
        "    os._exit(0)\n"
        "time.sleep(300)\n",
        encoding="utf-8",
    )
    command = [sys.executable, str(launcher), str(pid_file)]

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        parsers._run_child(command, shell=False, timeout=1.0)
    elapsed = time.monotonic() - started

    assert elapsed < 30, "超时后没有及时返回"

    deadline = time.monotonic() + 5
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pid_file.exists(), "孙进程没有启动，用例前提不成立"
    grandchild = int(pid_file.read_text(encoding="utf-8").strip())

    # 给内核一点时间回收
    for _ in range(50):
        if not _pid_alive(grandchild):
            break
        time.sleep(0.1)

    assert not _pid_alive(grandchild), (
        f"孙进程 {grandchild} 在超时后仍然存活：说明只杀了直接子进程，"
        "残留的进程树会逐步耗尽容器的 fork 能力"
    )


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
def test_normal_completion_returns_output(tmp_path: Path) -> None:
    """正常结束时不该被误杀，且 stdout/stderr 要能拿到。"""
    parsers = _parsers_module()

    result = parsers._run_child(
        "echo out; echo err >&2",
        shell=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "out" in result.stdout
    assert "err" in result.stderr


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
def test_failing_child_returns_nonzero_instead_of_raising(tmp_path: Path) -> None:
    """非零退出要原样返回给调用方判断，而不是抛异常。"""
    parsers = _parsers_module()

    result = parsers._run_child("exit 3", shell=True, timeout=10)

    assert result.returncode == 3


class TestThreadBudget:
    """解析子进程不能按宿主机核数开线程。

    容器用 ``cpus`` 配额而不是 CPU 亲和性，进程看到的仍是宿主机核数（线上 256）。
    PyTorch / OpenMP / MKL 不认配额，会按 256 开线程；线程也计入 cgroup 的
    ``pids``，于是一次 MinerU 解析就能吃掉 256 个任务，几次并发就把配额耗尽，
    随后所有 ``fork`` 失败（``soffice: Cannot fork``），``asyncio.to_thread``
    也跟着失败（``can't start new thread``），整个文档列表端点全挂。
    """

    def test_child_env_caps_openmp_family(self, monkeypatch) -> None:
        parsers = _parsers_module()
        for name in (
            "OMP_NUM_THREADS",
            "MKL_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
            "VECLIB_MAXIMUM_THREADS",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(parsers, "_cpu_budget", lambda: 4)

        env = parsers._child_env()

        for name in (
            "OMP_NUM_THREADS",
            "MKL_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
            "VECLIB_MAXIMUM_THREADS",
        ):
            assert env[name] == "4", f"{name} 没有按配额收敛，PyTorch 会按宿主机核数开线程"

    def test_operator_setting_wins(self, monkeypatch) -> None:
        """运维显式设了值就不要覆盖 —— 他们可能更清楚负载。"""
        parsers = _parsers_module()
        monkeypatch.setenv("OMP_NUM_THREADS", "2")
        monkeypatch.setattr(parsers, "_cpu_budget", lambda: 16)

        env = parsers._child_env()

        assert env["OMP_NUM_THREADS"] == "2"
        assert env["MKL_NUM_THREADS"] == "16"

    def test_cpu_budget_is_at_least_one(self) -> None:
        parsers = _parsers_module()
        assert parsers._cpu_budget() >= 1

    @pytest.mark.skipif(os.name != "posix", reason="POSIX-only")
    def test_child_process_actually_receives_the_cap(self, monkeypatch) -> None:
        """接线测试：上限必须真的传给子进程，而不只是 ``_child_env`` 会算。

        只测 ``_child_env()`` 的返回值是不够的 —— 把 ``env=`` 参数从
        ``Popen(...)`` 里删掉，那些用例照样通过，而子进程依旧按宿主机核数开线程。
        """
        parsers = _parsers_module()
        monkeypatch.setattr(parsers, "_cpu_budget", lambda: 7)
        monkeypatch.delenv("OMP_NUM_THREADS", raising=False)

        result = parsers._run_child(
            "printf '%s' \"$OMP_NUM_THREADS\"",
            shell=True,
            timeout=10,
        )

        assert result.stdout.strip() == "7", (
            f"子进程拿到的 OMP_NUM_THREADS 是 {result.stdout.strip()!r}，"
            "说明 _child_env() 没有传进 Popen"
        )
