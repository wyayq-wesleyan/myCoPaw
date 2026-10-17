# -*- coding: utf-8 -*-
"""Named I/O pools must isolate traffic and stay bounded.

Symptom this exists for: with one shared executor, a 4 MB session checkpoint
written every two seconds queued ahead of DB reads, so under load everything
queued behind everything else and no single owner was visible.

Pinned properties:

* work runs on the pool it was asked for (visible in the thread name, so a
  routing regression cannot hide);
* a saturated pool fails with a *readable* cause instead of an unbounded queue;
* a slot is released on success, on exception, and after a shielded
  cancellation — a leak here permanently shrinks the pool;
* saturating the file pool leaves the fast pool serving.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from qwenpaw.utils import io_pools
from qwenpaw.utils.io_pools import (
    POOL_FAST,
    POOL_FILE,
    POOL_SUBPROCESS,
    ThreadPoolBusy,
    install_io_pools,
    io_pool_stats,
    run_in_pool,
    shutdown_io_pools,
)


@pytest.fixture(autouse=True)
async def _fresh_pools(monkeypatch):
    """Give each test its own pools so sizes/waits can be set per test."""
    monkeypatch.setenv("QWENPAW_IO_POOL_WAIT_SECONDS", "0.4")
    await shutdown_io_pools()
    install_io_pools()
    try:
        yield
    finally:
        await shutdown_io_pools()
        # Leave no overrides behind for other tests.
        for name in (
            "QWENPAW_IO_FAST_THREADS",
            "QWENPAW_IO_FILE_THREADS",
            "QWENPAW_IO_SUBPROCESS_THREADS",
            "QWENPAW_IO_POOL_WAIT_SECONDS",
        ):
            monkeypatch.delenv(name, raising=False)


def _thread_name() -> str:
    return threading.current_thread().name


async def test_work_runs_on_the_requested_pool():
    """The pool name is what makes isolation observable in a stack dump."""
    for pool, prefix in (
        (POOL_FAST, "qp-io-fast"),
        (POOL_FILE, "qp-io-file"),
        (POOL_SUBPROCESS, "qp-io-subprocess"),
    ):
        name = await run_in_pool(pool, _thread_name)
        assert prefix in name, f"{pool} work ran on {name!r}"


async def test_unknown_pool_name_is_rejected():
    """A typo must fail loudly rather than silently pick the wrong pool."""
    with pytest.raises(ValueError, match="unknown I/O pool"):
        await run_in_pool("fiel", _thread_name)


async def test_saturation_reports_a_readable_cause():
    """Past workers+queue the caller gets a cause, not an open-ended queue."""
    release = threading.Event()
    entered = threading.Semaphore(0)

    def blocking() -> str:
        entered.release()
        release.wait(10)
        return "done"

    # File pool is 8 workers + 32 queue by default; shrink it so the test is
    # fast and deterministic.
    await shutdown_io_pools()
    io_pools._pools = {
        POOL_FILE: io_pools._IoPool(
            name=POOL_FILE, max_workers=1, queue_depth=1, wait_seconds=0.3,
        ),
    }
    io_pools._pools_loop_id = id(asyncio.get_running_loop())

    tasks = [
        asyncio.create_task(run_in_pool(POOL_FILE, blocking))
        for _ in range(2)
    ]
    # Capacity admits both submissions (1 worker + 1 queue slot), but only the
    # single worker thread reaches the body — that asymmetry is the point of
    # counting queue depth separately from workers.
    assert await asyncio.to_thread(entered.acquire, True, 5)

    try:
        with pytest.raises(ThreadPoolBusy) as excinfo:
            await run_in_pool(POOL_FILE, lambda: "never")
        message = str(excinfo.value)
        # The message has to name the pool and the numbers, otherwise the
        # operator cannot tell which limit to raise.
        assert POOL_FILE in message
        assert "1 个工作线程" in message
        assert "1 个排队位" in message
    finally:
        release.set()
        await asyncio.gather(*tasks)


async def test_slot_is_released_after_success_and_failure():
    """A leaked slot would shrink the pool for the process lifetime."""
    for _ in range(5):
        assert await run_in_pool(POOL_FILE, lambda: "ok") == "ok"

    def boom() -> None:
        raise RuntimeError("expected")

    for _ in range(5):
        with pytest.raises(RuntimeError):
            await run_in_pool(POOL_FILE, boom)

    stats = {row["pool"]: row for row in io_pool_stats()}
    assert stats[POOL_FILE]["active"] == 0
    assert stats[POOL_FILE]["waiting"] == 0
    assert stats[POOL_FILE]["submitted"] == 10


async def test_slot_survives_a_shielded_cancellation():
    """Cancelling the awaiter must not free the slot while the thread runs."""
    started = threading.Event()
    release = threading.Event()

    def blocking() -> None:
        started.set()
        release.wait(10)

    task = asyncio.create_task(run_in_pool(POOL_FILE, blocking))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    # Let the cancellation be delivered while the worker is still blocked, then
    # inspect the accounting from this coroutine — the cancelling task
    # itself is busy draining the thread by design, so it cannot report this.
    await asyncio.sleep(0.05)
    try:
        stats = {row["pool"]: row for row in io_pool_stats()}
        assert stats[POOL_FILE]["active"] == 1, (
            "the slot was released while the thread was still running; "
            "another coroutine could then overlap the same blocking work"
        )
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if {r["pool"]: r for r in io_pool_stats()}[POOL_FILE]["active"] == 0:
            break
        await asyncio.sleep(0.02)
    assert {r["pool"]: r for r in io_pool_stats()}[POOL_FILE]["active"] == 0


async def test_saturating_the_file_pool_leaves_the_fast_pool_serving():
    """This is the whole reason for separate pools."""
    release = threading.Event()
    entered = threading.Semaphore(0)

    def blocking() -> None:
        entered.release()
        release.wait(10)

    await shutdown_io_pools()
    io_pools._pools = {
        POOL_FILE: io_pools._IoPool(
            name=POOL_FILE, max_workers=1, queue_depth=0, wait_seconds=0.2,
        ),
        POOL_FAST: io_pools._IoPool(
            name=POOL_FAST, max_workers=2, queue_depth=0, wait_seconds=0.2,
        ),
    }
    io_pools._pools_loop_id = id(asyncio.get_running_loop())

    hog = asyncio.create_task(run_in_pool(POOL_FILE, blocking))
    assert await asyncio.to_thread(entered.acquire, True, 5)
    try:
        # The file pool is full and the fast pool must not care.
        assert await run_in_pool(POOL_FAST, lambda: "served") == "served"
    finally:
        release.set()
        await hog


async def test_run_sync_io_routes_by_pool():
    """``run_sync_io`` is the call site every helper funnels through."""
    from qwenpaw.utils.io_utils import run_sync_io

    assert "qp-io-fast" in await run_sync_io(_thread_name)
    assert "qp-io-file" in await run_sync_io(_thread_name, pool=POOL_FILE)
    assert (
        "qp-io-subprocess"
        in await run_sync_io(_thread_name, pool=POOL_SUBPROCESS)
    )


async def test_pool_work_sees_the_caller_contextvars():
    """Pool threads must inherit request-scoped context.

    ``loop.run_in_executor`` does not copy contextvars (``asyncio.to_thread``
    does). Losing them here is silent: request-scoped account/workspace lookups
    read as their default inside the worker, which is how shared state gets
    attributed to the wrong tenant.
    """
    import contextvars

    marker: contextvars.ContextVar[str] = contextvars.ContextVar(
        "test_pool_ctx_marker",
        default="UNSET",
    )

    def read_marker() -> str:
        return marker.get()

    marker.set("in-request")
    assert await run_in_pool(POOL_FAST, read_marker) == "in-request"
    assert await run_in_pool(POOL_FILE, read_marker) == "in-request"
    assert await run_in_pool(POOL_SUBPROCESS, read_marker) == "in-request"


async def test_without_install_it_falls_back_instead_of_failing():
    """CLIs and unit tests use these helpers without the app lifespan."""
    await shutdown_io_pools()
    assert await run_in_pool(POOL_FAST, lambda: 7) == 7
    assert io_pool_stats() == []
