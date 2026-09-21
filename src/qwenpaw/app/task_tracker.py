# -*- coding: utf-8 -*-
"""Task tracker for background runs: streaming, reconnect, multi-subscriber.

``run_key`` is typically ``ChatSpec.id`` (chat_id). Per run: task, queues,
event buffer. Reconnects get buffer replay + new events. Cleanup when task
completes.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import weakref
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Callable, Coroutine, Optional

from ..constant import WORKING_DIR
from ..utils.io_utils import read_json, run_sync_io, write_json_atomic

logger = logging.getLogger(__name__)

_SENTINEL = None

# Keep a browser/proxy SSE connection alive while the agent is waiting for a
# model, document parser, or tool.  This is intentionally per-subscriber and
# is never added to the replay buffer or persisted chat history.
try:
    _SSE_HEARTBEAT_SECONDS = max(
        1.0,
        float(os.environ.get("QWENPAW_SSE_HEARTBEAT_SECONDS", "10")),
    )
except (TypeError, ValueError):
    _SSE_HEARTBEAT_SECONDS = 10.0
SSE_HEARTBEAT = f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

# Emitted to reconnect subscribers right after the buffered events, so
# the client can render the replayed part instantly (no token-by-token
# re-animation) and switch to live streaming afterwards.
REPLAY_END_SSE = f"data: {json.dumps({'type': 'replay_end'})}\n\n"
_COMPLETED_RUN_TTL_SECONDS = max(
    60.0,
    float(os.environ.get("QWENPAW_COMPLETED_RUN_TTL_SECONDS", "600")),
)
_MAX_COMPLETED_RUNS = max(
    10,
    int(os.environ.get("QWENPAW_MAX_COMPLETED_RUNS", "200")),
)
_MAX_RUN_BUFFER_EVENTS = max(
    100,
    int(os.environ.get("QWENPAW_MAX_RUN_BUFFER_EVENTS", "5000")),
)
_PERSIST_INTERVAL_SECONDS = max(
    0.5,
    float(os.environ.get("QWENPAW_RUN_REPLAY_PERSIST_SECONDS", "2")),
)
_REPLAY_DIR = WORKING_DIR / "chat_replays"


@dataclass
class _RunState:
    """Per-run state (task, queues, buffer), guarded by tracker lock."""

    task: asyncio.Future
    queues: list[asyncio.Queue] = field(default_factory=list)
    buffer: list[str] = field(default_factory=list)
    start_time: Optional[datetime] = None
    finish_time: Optional[datetime] = None
    owner: object | None = None
    retained_until: float | None = None
    last_persist_at: float = 0.0


def _replay_path(run_key: str):
    # Chat IDs are normally UUIDs, but hash defensively so an external caller
    # can never turn a run key into a filesystem path.
    digest = hashlib.sha256(run_key.encode("utf-8")).hexdigest()
    return _REPLAY_DIR / f"{digest}.json"


def _write_replay(
    run_key: str,
    buffer: list[str],
    expires_at: float,
) -> None:
    path = _replay_path(run_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        path,
        {
            "run_key": run_key,
            "expires_at": expires_at,
            "events": buffer,
        },
        sort_keys=True,
    )


def _read_replay(run_key: str) -> list[str] | None:
    path = _replay_path(run_key)
    if not path.exists():
        return None
    try:
        payload = read_json(path)
        if (
            not isinstance(payload, dict)
            or payload.get("run_key") != run_key
            or float(payload.get("expires_at", 0)) <= time.time()
        ):
            path.unlink(missing_ok=True)
            return None
        events = payload.get("events")
        if not isinstance(events, list):
            return None
        return [item for item in events if isinstance(item, str)]
    except Exception:  # pylint: disable=broad-except
        logger.warning("failed to read chat replay for run=%s", run_key)
        return None


def _delete_replay(run_key: str) -> None:
    _replay_path(run_key).unlink(missing_ok=True)


def prune_replay_directory(max_age_seconds: float | None = None) -> int:
    """Delete replay files older than the TTL, whatever their run key.

    Expiry is otherwise only applied when a file is *read* (``_read_replay``) or
    overwritten by a new run for the same chat. A chat that is never revisited
    therefore keeps its replay forever, so the directory grows with the number
    of chats ever used rather than with active ones. This sweep bounds it.

    Returns the number of files removed. Best-effort: a failure here must never
    affect a request.
    """
    ttl = (
        _COMPLETED_RUN_TTL_SECONDS
        if max_age_seconds is None
        else float(max_age_seconds)
    )
    removed = 0
    try:
        if not _REPLAY_DIR.is_dir():
            return 0
        cutoff = time.time() - ttl
        for entry in _REPLAY_DIR.iterdir():
            if not entry.is_file() or entry.suffix != ".json":
                continue
            try:
                if entry.stat().st_mtime >= cutoff:
                    continue
                entry.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
    except Exception:  # pylint: disable=broad-except
        logger.warning("failed to prune chat replays", exc_info=True)
        return removed
    if removed:
        logger.info("pruned %d expired chat replay file(s)", removed)
    return removed


class TaskTracker:
    """Per-agent tracker: run_key -> RunState.

    All mutations to _runs under _lock. Producer broadcasts under lock.
    Subscribers use unbounded per-connection queues; disconnect removes them
    via :meth:`detach_subscriber`. A workspace reload reuses the same tracker
    so active runs remain reconnectable.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._runs: dict[str, _RunState] = {}
        self._global_last_run_at: Optional[datetime] = None
        self._global_last_finish_at: Optional[datetime] = None

    def _purge_completed_locked(self) -> None:
        """Drop expired/old completed buffers while retaining recent replays."""
        now = time.monotonic()
        completed = [
            (key, state)
            for key, state in self._runs.items()
            if state.task.done()
        ]
        for key, state in completed:
            if state.retained_until is not None and state.retained_until <= now:
                self._runs.pop(key, None)
        completed = [
            (key, state)
            for key, state in self._runs.items()
            if state.task.done()
        ]
        if len(completed) > _MAX_COMPLETED_RUNS:
            completed.sort(key=lambda item: item[1].retained_until or 0)
            for key, _state in completed[:-_MAX_COMPLETED_RUNS]:
                self._runs.pop(key, None)

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    async def get_status(self, run_key: str) -> str:
        """Return ``'idle'`` or ``'running'``."""
        async with self._lock:
            self._purge_completed_locked()
            state = self._runs.get(run_key)
        if state is None:
            return "idle"
        return "idle" if state.task.done() else "running"

    async def get_global_status(self) -> dict:
        """Get global agent status summary.

        Returns:
            dict with keys:
                - status: 'idle' | 'running'
                - running_task_count: int
                - last_run_at: Optional[datetime]
                - last_finish_at: Optional[datetime]
        """
        async with self._lock:
            running_count = sum(
                1 for state in self._runs.values() if not state.task.done()
            )
            status = "running" if running_count > 0 else "idle"

            return {
                "status": status,
                "running_task_count": running_count,
                "last_run_at": self._global_last_run_at,
                "last_finish_at": self._global_last_finish_at,
            }

    async def has_active_tasks(self) -> bool:
        """Check if any tasks are currently running.

        Returns:
            bool: True if any tasks are active, False otherwise
        """
        async with self._lock:
            for state in self._runs.values():
                if not state.task.done():
                    return True
            return False

    async def has_active_tasks_excluding(
        self,
        excluded_task: asyncio.Future | None,
    ) -> bool:
        """Check for active tasks other than ``excluded_task``."""
        async with self._lock:
            return any(
                not state.task.done() and state.task is not excluded_task
                for state in self._runs.values()
            )

    async def list_active_tasks(self) -> list[str]:
        """List all currently running task keys.

        Returns:
            list[str]: List of active run_keys
        """
        async with self._lock:
            return [
                run_key
                for run_key, state in self._runs.items()
                if not state.task.done()
            ]

    async def snapshot_active_tasks(
        self,
        owner: object | None = None,
    ) -> dict[str, asyncio.Future]:
        """Return the currently active tasks without tracking later runs."""
        async with self._lock:
            return {
                run_key: state.task
                for run_key, state in self._runs.items()
                if not state.task.done()
                and (owner is None or state.owner is owner)
            }

    async def wait_tasks_done(
        self,
        tasks: list[asyncio.Future],
        timeout: float = 300.0,
    ) -> bool:
        """Wait for a fixed task snapshot without cancelling on timeout."""
        if not tasks:
            return True

        async def _wait_snapshot() -> None:
            await asyncio.gather(
                *(asyncio.shield(task) for task in tasks),
                return_exceptions=True,
            )

        try:
            await asyncio.wait_for(_wait_snapshot(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_all_done(self, timeout: float = 300.0) -> bool:
        """Wait for all active tasks to complete.

        Args:
            timeout: Maximum time to wait in seconds (default: 300s = 5min)

        Returns:
            bool: True if all tasks completed, False if timeout occurred
        """

        async def _wait_loop() -> None:
            while await self.has_active_tasks():
                await asyncio.sleep(0.5)

        try:
            await asyncio.wait_for(_wait_loop(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def attach(self, run_key: str) -> asyncio.Queue | None:
        """Attach to an existing run.

        Returns a new queue pre-filled with the event buffer plus a
        ``replay_end`` marker, or ``None`` if no run is active for
        *run_key*.
        """
        async with self._lock:
            self._purge_completed_locked()
            state = self._runs.get(run_key)
            if state is not None:
                q: asyncio.Queue = asyncio.Queue()
                for sse in state.buffer:
                    q.put_nowait(sse)
                q.put_nowait(REPLAY_END_SSE)
                state.queues.append(q)
                if state.task.done():
                    q.put_nowait(_SENTINEL)
                return q

        # A process restart loses in-memory queues.  The producer writes a
        # bounded checkpoint every few seconds, so reconnect can still replay
        # the completed/interrupted turn for the retention window.
        persisted = await run_sync_io(_read_replay, run_key)
        if persisted is None:
            return None
        q = asyncio.Queue()
        for sse in persisted:
            q.put_nowait(sse)
        q.put_nowait(REPLAY_END_SSE)
        q.put_nowait(_SENTINEL)
        return q

    async def detach_subscriber(
        self,
        run_key: str,
        queue: asyncio.Queue,
    ) -> None:
        """Remove *queue* from *run_key*'s subscriber list.

        Idempotent if the run ended or *queue* was already removed.
        """
        async with self._lock:
            state = self._runs.get(run_key)
            if state is None:
                return
            try:
                state.queues.remove(queue)
            except ValueError:
                pass

    async def request_stop(self, run_key: str) -> bool:
        """Cancel the run. Returns ``True`` if it was running."""
        logger.debug("[STOP] request_stop called for run_key=%s", run_key)
        async with self._lock:
            state = self._runs.get(run_key)
            logger.debug(
                "[STOP] run_key=%s state=%s done=%s",
                run_key,
                "found" if state else "not_found",
                state.task.done() if state else "N/A",
            )
            if state is None or state.task.done():
                logger.debug(
                    "[STOP] Cannot stop run_key=%s (not running)",
                    run_key,
                )
                return False
            logger.debug(
                "[STOP] Calling task.cancel() for run_key=%s",
                run_key,
            )
            task = state.task
            task.cancel()
            logger.debug("[STOP] task.cancel() called for run_key=%s", run_key)
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    async def attach_or_start(
        self,
        run_key: str,
        payload: Any,
        stream_fn: Callable[..., Coroutine],
        owner: object | None = None,
    ) -> tuple[asyncio.Queue, bool]:
        """Attach to an existing run or start a new one.

        Returns ``(queue, is_new_run)``.
        """
        async with self._lock:
            self._purge_completed_locked()
            state = self._runs.get(run_key)
            if state is not None and not state.task.done():
                q: asyncio.Queue = asyncio.Queue()
                for sse in state.buffer:
                    q.put_nowait(sse)
                state.queues.append(q)
                return q, False

            my_queue: asyncio.Queue = asyncio.Queue()
            run = _RunState(
                task=asyncio.Future(),  # placeholder, replaced below
                queues=[my_queue],
                buffer=[],
                owner=owner,
            )
            self._runs[run_key] = run

            # A new user question supersedes any recoverable replay from the
            # prior completed turn in the same chat.
            await run_sync_io(_delete_replay, run_key)

            tracker_ref = weakref.ref(self)

            async def _producer() -> None:
                start_time = datetime.now(timezone.utc)

                try:
                    tracker = tracker_ref()
                    if tracker is not None:
                        async with tracker.lock:
                            run.start_time = start_time
                            # pylint: disable=protected-access
                            tracker._global_last_run_at = start_time

                    async for sse in stream_fn(payload):
                        tracker = tracker_ref()
                        if tracker is None:
                            return
                        async with tracker.lock:
                            run.buffer.append(sse)
                            if len(run.buffer) > _MAX_RUN_BUFFER_EVENTS:
                                del run.buffer[:-_MAX_RUN_BUFFER_EVENTS]
                            for q in run.queues:
                                q.put_nowait(sse)
                            now = time.monotonic()
                            if (
                                now - run.last_persist_at
                                >= _PERSIST_INTERVAL_SECONDS
                            ):
                                run.last_persist_at = now
                                snapshot = list(run.buffer)
                            else:
                                snapshot = None
                        if snapshot is not None:
                            await run_sync_io(
                                _write_replay,
                                run_key,
                                snapshot,
                                time.time() + _COMPLETED_RUN_TTL_SECONDS,
                            )
                except asyncio.CancelledError:
                    logger.debug("run cancelled run_key=%s", run_key)
                except Exception:
                    logger.exception("run error run_key=%s", run_key)
                    err_sse = (
                        "data: "
                        f"{json.dumps({'error': 'internal server error'})}\n\n"
                    )
                    tracker = tracker_ref()
                    if tracker is not None:
                        async with tracker.lock:
                            run.buffer.append(err_sse)
                            for q in run.queues:
                                q.put_nowait(err_sse)
                finally:
                    finish_time = datetime.now(timezone.utc)
                    tracker = tracker_ref()
                    if tracker is not None:
                        final_snapshot = None
                        async with tracker.lock:
                            run.finish_time = finish_time
                            # pylint: disable=protected-access
                            tracker._global_last_finish_at = finish_time
                            for q in run.queues:
                                q.put_nowait(_SENTINEL)
                            # pylint: disable=protected-access
                            # Keep a bounded replay after completion so a page
                            # refresh can recover the just-finished answer.
                            run.retained_until = (
                                time.monotonic() + _COMPLETED_RUN_TTL_SECONDS
                            )
                            final_snapshot = list(run.buffer)
                            tracker._purge_completed_locked()
                        if final_snapshot:
                            await run_sync_io(
                                _write_replay,
                                run_key,
                                final_snapshot,
                                time.time() + _COMPLETED_RUN_TTL_SECONDS,
                            )

            run.task = asyncio.create_task(_producer())
            return my_queue, True

    async def stream_from_queue(
        self,
        queue: asyncio.Queue,
        run_key: str,
    ) -> AsyncGenerator[str, None]:
        """Yield SSE strings from *queue* until the sentinel ``None``.

        Always detaches *queue* from *run_key* when this stream ends or is
        closed (including client disconnect), so reconnects do not leak queues.
        """
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(),
                        timeout=_SSE_HEARTBEAT_SECONDS,
                    )
                    if event is _SENTINEL:
                        break
                    yield event
                except asyncio.TimeoutError:
                    # A data event, rather than an SSE comment, also resets
                    # SDK-level idle timers. The web client treats this as a
                    # no-op heartbeat.
                    yield SSE_HEARTBEAT
                except asyncio.CancelledError:
                    break
        finally:
            await self.detach_subscriber(run_key, queue)
