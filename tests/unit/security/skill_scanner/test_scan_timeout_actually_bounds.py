# -*- coding: utf-8 -*-
"""技能扫描的「超时」必须真的限时，否则建技能能卡死整个服务。

原实现：

    with futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(scanner.scan_skill, ...)
        try:
            result = future.result(timeout=effective_timeout)
        except futures.TimeoutError:
            future.cancel()
            return None

`ThreadPoolExecutor.__exit__` 等价于 `shutdown(wait=True)`，会**等那个卡住的
扫描线程**。`future.cancel()` 对已在运行的任务返回 False，什么也没取消。实测：
0.5s 的超时实际阻塞 3.0s；扫描若永久挂起，调用方就永久阻塞。

而调用方是 `async def create_skill` / `save_workspace_skill` 里的同步调用，
阻塞的就是事件循环 —— 建一个技能就能让全站（含 /healthz）停住，和线上那次
「pids 耗尽 → 整页 500」是同一类体感。

修法是手动创建 executor 并 `shutdown(wait=False)`，让超时真的返回。
"""
from __future__ import annotations

import time
from concurrent import futures

from qwenpaw.security import skill_scanner


def _method_source(name: str) -> str:
    import inspect

    return inspect.getsource(getattr(skill_scanner, name))


class TestTimeoutIsNotDefeatedByShutdownWait:
    def test_scanner_does_not_wait_for_the_abandoned_thread(self) -> None:
        """源码契约：执行器必须以 wait=False 关闭。

        断言前必须**剥掉注释**：这段代码的注释里本身就写着
        ``shutdown(wait=False)``（解释为什么这么写），直接搜全文的话，把代码
        改回 ``wait=True`` 而注释保留，断言照样通过 —— 测试读的是解释而不是行为。
        """
        import inspect
        import re

        src = inspect.getsource(skill_scanner)
        code = "\n".join(
            line.split("#", 1)[0] for line in src.splitlines()
        )
        assert "shutdown(wait=False)" in code, (
            "执行器又变回阻塞关闭了：超时会被 __exit__ 的 wait=True 抵消"
        )
        # 不能再出现 with ThreadPoolExecutor(...) 包住 future.result(timeout=) 的写法
        assert not re.search(
            r"with futures\.ThreadPoolExecutor\([^)]*\)\s*as executor:\s*\n\s*future = executor\.submit",
            src,
        ), "扫描又回到了 `with ThreadPoolExecutor` 写法：超时不生效"


class TestThePatternItself:
    """把「with 会让超时失效」这件事钉成可执行的事实。

    这条不测产品代码，测的是**我们依赖的语言行为**：如果哪天 Python 改成
    __exit__ 不等待，上一条源码断言就该重新评估。
    """

    def test_with_block_waits_for_a_stuck_task(self) -> None:
        started = time.monotonic()
        with futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(time.sleep, 1.0)
            try:
                future.result(timeout=0.05)
            except futures.TimeoutError:
                assert future.cancel() is False, (
                    "运行中的任务应当无法取消；此断言用于确认用例前提"
                )
        waited = time.monotonic() - started
        assert waited >= 0.9, (
            f"预期 with 块会等满 1s，实际 {waited:.2f}s —— "
            "如果这个变了，需要重新评估 skill_scanner 的超时写法"
        )

    def test_shutdown_without_wait_returns_immediately(self) -> None:
        started = time.monotonic()
        executor = futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(time.sleep, 1.0)
            try:
                future.result(timeout=0.05)
            except futures.TimeoutError:
                future.cancel()
        finally:
            executor.shutdown(wait=False)
        elapsed = time.monotonic() - started
        assert elapsed < 0.5, (
            f"shutdown(wait=False) 应立刻返回，实际 {elapsed:.2f}s"
        )
