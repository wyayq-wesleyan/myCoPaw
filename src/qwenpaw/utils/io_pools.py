# -*- coding: utf-8 -*-
"""Purpose-separated thread pools for blocking I/O.

Why this module exists
----------------------
Every synchronous I/O call used to share one default executor (``to_thread`` →
``min(32, cpu+4)`` threads). A 4 MB session-checkpoint write therefore competed
with a connection-status read, and under load everything queued behind
everything else. Worse, there was no way to see who held the threads.

Four named pools separate the traffic by shape:

* ``fast``       — DB queries and config reads: short and latency sensitive.
* ``file``       — session checkpoints, remote downloads, large file writes.
* ``subprocess`` — shell / git / soffice / child processes (shell tool's
  Windows sync path, chromium spawn/kill, short-lived ``run_command_async``).
* ``audit``      — governance audit writes only: a single lane that cannot be
  starved by, and cannot starve, any other pool.

Isolation, not more threads, is the point: file writes can no longer starve DB
reads. The default executor is deliberately left alone — 250 call sites still
use ``asyncio.to_thread`` directly, and re-pointing the default at a smaller
pool would silently halve their capacity. Call sites still using the default
executor for child processes (browser tool internals, ``proc.wait`` waits) are
not observable through these pools — moving them is an explicit follow-up, not
done silently here.

Backpressure
------------
Each pool admits ``max_workers + queue_depth`` concurrent submissions through
an :class:`asyncio.Semaphore`. Past that, callers wait up to
:func:`pool_wait_seconds` and then receive :class:`ThreadPoolBusy` with a
readable message — instead of an unbounded queue whose overflow only surfaces
later as an unrelated timeout. The audit lane has its own, much shorter
budget (:func:`audit_wait_seconds`) because a tool call must not stall for
minutes behind bookkeeping.

Off-loop only
-------------
Submitting a job never blocks: awaiting the handle is what waits. That matters
because the callers include policy auditing, which previously performed a
synchronous ``INSERT`` + ``commit`` (and, when the platform pool was full, a
15-second wait) directly on the event loop.

Tuning
------
``QWENPAW_IO_FAST_THREADS`` / ``..._FILE_THREADS`` / ``..._SUBPROCESS_THREADS``
/ ``..._AUDIT_THREADS``, plus ``QWENPAW_IO_POOL_WAIT_SECONDS`` and
``QWENPAW_IO_AUDIT_WAIT_SECONDS``. Defaults target an 8–10 user deployment;
:func:`io_pool_stats` exists so they can be adjusted from measurements rather
than guesses.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

POOL_FAST = "fast"
POOL_FILE = "file"
POOL_SUBPROCESS = "subprocess"
# Governance audit writes get their own single-thread lane. An audit record is
# INSERT + commit behind a process-wide RLock, so one worker drains the lane at
# the database's pace and nothing else can starve it; without a dedicated lane,
# audit traffic waiting on the fast pool can delay tool calls by the general
# wait budget, and fast-pool saturation loses audit records wholesale.
POOL_AUDIT = "audit"

POOL_NAMES: tuple[str, ...] = (
    POOL_FAST,
    POOL_FILE,
    POOL_SUBPROCESS,
    POOL_AUDIT,
)

_DEFAULT_WORKERS: dict[str, int] = {
    POOL_FAST: 16,
    POOL_FILE: 8,
    POOL_SUBPROCESS: 8,
    POOL_AUDIT: 1,
}
_DEFAULT_QUEUE_DEPTH: dict[str, int] = {
    POOL_FAST: 64,
    POOL_FILE: 32,
    POOL_SUBPROCESS: 16,
    POOL_AUDIT: 256,
}
_WORKER_ENV: dict[str, str] = {
    POOL_FAST: "QWENPAW_IO_FAST_THREADS",
    POOL_FILE: "QWENPAW_IO_FILE_THREADS",
    POOL_SUBPROCESS: "QWENPAW_IO_SUBPROCESS_THREADS",
    POOL_AUDIT: "QWENPAW_IO_AUDIT_THREADS",
}
_WAIT_ENV = "QWENPAW_IO_POOL_WAIT_SECONDS"
_DEFAULT_WAIT_SECONDS = 120.0
# Audit must not stall a tool call anywhere near the general wait budget: if
# the lane is still jammed after 30s, the record is dropped with an error log
# (audit is best-effort by contract) and the tool call proceeds.
_AUDIT_WAIT_ENV = "QWENPAW_IO_AUDIT_WAIT_SECONDS"
_DEFAULT_AUDIT_WAIT_SECONDS = 30.0

_T = TypeVar("_T")


class ThreadPoolBusy(RuntimeError):
    """A named pool stayed saturated for the whole wait budget.

    Raised instead of queueing without bound, so the caller can report a cause
    (``"文件写入线程池繁忙（8 线程 + 32 排队已满）"``) rather than degrade into an
    unattributed timeout somewhere else.
    """


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric %s=%r", name, raw)
        return default
    if value <= 0:
        logger.warning("Ignoring non-positive %s=%r", name, raw)
        return default
    return value


def pool_wait_seconds() -> float:
    """Seconds a caller may wait for a pool slot before giving up."""
    raw = os.environ.get(_WAIT_ENV, "").strip()
    if not raw:
        return _DEFAULT_WAIT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric %s=%r", _WAIT_ENV, raw)
        return _DEFAULT_WAIT_SECONDS
    return value if value > 0 else _DEFAULT_WAIT_SECONDS


def audit_wait_seconds() -> float:
    """Wait budget for the dedicated audit lane (shorter than the general one
    by design: a tool call must not stall minutes on an audit write)."""
    raw = os.environ.get(_AUDIT_WAIT_ENV, "").strip()
    if not raw:
        return _DEFAULT_AUDIT_WAIT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric %s=%r", _AUDIT_WAIT_ENV, raw)
        return _DEFAULT_AUDIT_WAIT_SECONDS
    return value if value > 0 else _DEFAULT_AUDIT_WAIT_SECONDS


class _IoPool:
    """One bounded thread pool with admission control and counters."""

    def __init__(
        self,
        name: str,
        max_workers: int,
        queue_depth: int,
        wait_seconds: float,
    ) -> None:
        self.name = name
        self.max_workers = max_workers
        self.queue_depth = queue_depth
        self.wait_seconds = wait_seconds
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"qp-io-{name}",
        )
        self._semaphore = asyncio.Semaphore(max_workers + queue_depth)
        # Counters are plain integers: bounded by construction, no collection
        # that could grow with traffic (see the "只增不删" defect class).
        self._lock = threading.Lock()
        self._active = 0
        self._waiting = 0
        self._peak_active = 0
        self._submitted = 0
        self._busy_rejections = 0

    @property
    def capacity(self) -> int:
        return self.max_workers + self.queue_depth

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "pool": self.name,
                "max_workers": self.max_workers,
                "queue_depth": self.queue_depth,
                "capacity": self.capacity,
                "active": self._active,
                "waiting": self._waiting,
                "peak_active": self._peak_active,
                "submitted": self._submitted,
                "busy_rejections": self._busy_rejections,
            }

    def _enter_waiting(self) -> None:
        with self._lock:
            self._waiting += 1

    def _leave_waiting(self) -> None:
        with self._lock:
            self._waiting -= 1

    def _enter_active(self) -> None:
        with self._lock:
            self._submitted += 1
            self._active += 1
            if self._active > self._peak_active:
                self._peak_active = self._active

    def _leave_active(self) -> None:
        with self._lock:
            self._active -= 1

    def _record_busy(self) -> None:
        with self._lock:
            self._busy_rejections += 1

    def _busy_error(self) -> ThreadPoolBusy:
        return ThreadPoolBusy(
            f"{self.name} 线程池繁忙：{self.max_workers} 个工作线程 + "
            f"{self.queue_depth} 个排队位已占满，等待 "
            f"{self.wait_seconds:.0f} 秒后仍未取得执行位。"
            "请稍后重试；若持续出现，请调大 "
            f"{_WORKER_ENV.get(self.name, 'QWENPAW_IO_*_THREADS')}。",
        )

    def _on_done(self, _future: Any) -> None:
        # Runs in the event loop (asyncio.Future callbacks are loop-scheduled),
        # so releasing the semaphore here is safe. Releasing on completion —
        # rather than in the awaiting coroutine's finally — is what keeps the
        # slot accounted for while a shielded job drains after cancellation.
        self._leave_active()
        self._semaphore.release()

    async def run(
        self,
        operation: Callable[..., _T],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> _T:
        loop = asyncio.get_running_loop()

        self._enter_waiting()
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                self.wait_seconds,
            )
        except (asyncio.TimeoutError, TimeoutError):
            self._record_busy()
            raise self._busy_error() from None
        finally:
            self._leave_waiting()

        self._enter_active()
        try:
            future = loop.run_in_executor(
                self._executor,
                # Run through a copy of the caller's context.
                # `run_in_executor` does NOT propagate contextvars the way
                # `asyncio.to_thread` does — without this, request-scoped
                # account/workspace/agent context silently reads as its
                # default inside the worker thread.
                partial(
                    contextvars.copy_context().run,
                    operation,
                    *args,
                    **kwargs,
                ),
            )
        except BaseException:
            # The loop refused the work (e.g. shutting down). Never leak the
            # slot, or the pool would lose capacity for the process lifetime.
            self._leave_active()
            self._semaphore.release()
            raise
        future.add_done_callback(self._on_done)

        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # A worker thread cannot be cancelled. Wait for it before letting
            # the caller release its path/domain lock, so no other coroutine
            # can overlap the still-running blocking operation.
            try:
                await future
            except BaseException:
                pass
            raise

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait)


_pools: dict[str, _IoPool] | None = None
_pools_loop_id: int | None = None
_pools_lock = threading.Lock()


def install_io_pools(
    loop: asyncio.AbstractEventLoop | None = None,
) -> dict[str, int]:
    """Create the pools for the running loop and return their sizes.

    Idempotent per loop. Called from application startup so the sizes are
    visible in the boot log; :func:`run_in_pool` falls back to the default
    executor when this was never called (unit tests, one-off scripts).
    """
    global _pools, _pools_loop_id
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
    with _pools_lock:
        loop_id = id(loop) if loop is not None else None
        if _pools is not None and _pools_loop_id == loop_id:
            return {name: pool.max_workers for name, pool in _pools.items()}
        if _pools is not None:
            for pool in _pools.values():
                pool.shutdown(wait=False)
        wait_seconds = pool_wait_seconds()
        audit_wait = audit_wait_seconds()
        _pools = {
            name: _IoPool(
                name=name,
                max_workers=_env_int(
                    _WORKER_ENV[name],
                    _DEFAULT_WORKERS[name],
                ),
                queue_depth=_DEFAULT_QUEUE_DEPTH[name],
                wait_seconds=(
                    audit_wait if name == POOL_AUDIT else wait_seconds
                ),
            )
            for name in POOL_NAMES
        }
        _pools_loop_id = loop_id
        return {name: pool.max_workers for name, pool in _pools.items()}


async def shutdown_io_pools() -> None:
    """Release every pool. Safe to call when nothing was installed."""
    global _pools, _pools_loop_id
    with _pools_lock:
        pools, _pools = _pools, None
        _pools_loop_id = None
    if not pools:
        return
    for pool in pools.values():
        try:
            pool.shutdown(wait=False)
        except Exception:  # noqa: BLE001 - shutdown must never raise
            logger.warning(
                "Could not shut down %s pool",
                pool.name,
                exc_info=True,
            )


def io_pool_stats() -> list[dict[str, Any]]:
    """Snapshot every pool. Read-only; never raises."""
    pools = _pools
    if not pools:
        return []
    result: list[dict[str, Any]] = []
    for name in POOL_NAMES:
        pool = pools.get(name)
        if pool is None:
            continue
        try:
            result.append(pool.stats())
        except Exception:  # noqa: BLE001 - reporting must never raise
            logger.debug("Could not read %s pool stats", name, exc_info=True)
    return result


async def _run_on_default_executor(
    operation: Callable[..., _T],
    /,
    *args: Any,
    **kwargs: Any,
) -> _T:
    """Original behaviour: the loop's default executor, shielded + drained."""
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except BaseException:
            pass
        raise


async def run_in_pool(
    pool_name: str,
    operation: Callable[..., _T],
    /,
    *args: Any,
    **kwargs: Any,
) -> _T:
    """Run ``operation`` in the named pool (default executor if not installed).

    ``pool_name`` is resolved before touching the operation, so a typo fails
    loudly instead of silently landing in the wrong pool.
    """
    if pool_name not in POOL_NAMES:
        raise ValueError(
            f"unknown I/O pool {pool_name!r}; "
            f"expected one of {', '.join(POOL_NAMES)}",
        )
    pools = _pools
    if pools is None:
        # Not installed (unit tests, CLIs). Keep the pre-existing semantics
        # rather than silently creating pools nobody will shut down.
        return await _run_on_default_executor(operation, *args, **kwargs)
    pool = pools.get(pool_name)
    if pool is None:
        return await _run_on_default_executor(operation, *args, **kwargs)
    return await pool.run(operation, *args, **kwargs)
