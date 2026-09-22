# -*- coding: utf-8 -*-
"""The SSE heartbeat must not swallow the wrapped iterator's own timeouts.

``_iter_with_heartbeat`` exists to emit keep-alive ticks while a run is idle,
and it used ``asyncio.wait_for`` to time the inner ``__anext__()``. Since
Python 3.11 ``asyncio.TimeoutError`` *is* ``TimeoutError``, and this project's
``StreamIdleTimeoutError`` / ``StreamCleanupPendingError`` subclass it — so the
``except asyncio.TimeoutError`` branch could not tell "my interval elapsed"
from "the model layer raised a timeout".

The consequences were severe and silent: the model's ``StreamIdleTimeoutError``
was converted into a heartbeat, the already-failed task was kept, and the next
loop iteration awaited that same task again and re-raised instantly. The result
was an unbounded tight loop — measured at ~2500 events/second, 53.9 MB of SSE
in 90 seconds — while the run never advanced and the real error never surfaced.

These tests pin the distinction: a timeout raised by the iterator propagates,
while a genuine idle interval still yields ticks.
"""
from __future__ import annotations

import asyncio

import pytest

from qwenpaw.runtime.heartbeat import _HEARTBEAT_TICK, _iter_with_heartbeat


async def _raising(exc: BaseException):
    await asyncio.sleep(0)
    raise exc
    yield  # pragma: no cover - makes this an async generator


async def _slow_values(count: int, delay: float, interval: float):
    for index in range(count):
        await asyncio.sleep(delay)
        yield index


async def test_stream_idle_timeout_propagates_instead_of_spinning() -> None:
    """The model layer's idle timeout must reach the caller."""
    from qwenpaw.providers.retry_chat_model import StreamIdleTimeoutError

    error = StreamIdleTimeoutError("prov:model", 30.0, "QWENPAW_X")
    ticks = 0
    with pytest.raises(StreamIdleTimeoutError):
        async for item in _iter_with_heartbeat(_raising(error), 0.05):
            if item is _HEARTBEAT_TICK:
                ticks += 1
                # A spin would keep yielding ticks forever; fail loudly rather
                # than hang the suite.
                assert ticks < 5, "heartbeat swallowed the timeout (spin)"
    assert ticks == 0


async def test_plain_timeout_error_propagates() -> None:
    """Any TimeoutError from the iterator is a real failure, not idleness."""
    with pytest.raises(TimeoutError):
        async for _ in _iter_with_heartbeat(
            _raising(TimeoutError("upstream read timed out")),
            0.05,
        ):
            pytest.fail("no tick should be emitted for a failing iterator")


async def test_idle_iterator_still_emits_heartbeats_then_values() -> None:
    """A slow-but-working iterator yields ticks and then its real values."""
    ticks = 0
    values = []
    async for item in _iter_with_heartbeat(_slow_values(2, 0.2, 0.05), 0.05):
        if item is _HEARTBEAT_TICK:
            ticks += 1
        else:
            values.append(item)

    assert values == [0, 1]
    assert ticks >= 1, "a slow iterator must keep the connection alive"


async def test_fast_iterator_emits_no_heartbeats() -> None:
    """Values arriving within the interval must not produce spurious ticks."""
    seen = []
    async for item in _iter_with_heartbeat(_slow_values(3, 0.0, 5.0), 5.0):
        assert item is not _HEARTBEAT_TICK
        seen.append(item)

    assert seen == [0, 1, 2]


async def test_abandoning_the_wrapper_cancels_the_inner_task() -> None:
    """Breaking out early must not leave the inner ``__anext__`` running."""
    cancelled = asyncio.Event()

    async def _very_slow():
        try:
            await asyncio.sleep(30)
            yield 1
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def _consume_one():
        async for _ in _iter_with_heartbeat(_very_slow(), 0.01):
            return

    await asyncio.wait_for(_consume_one(), timeout=5)
    await asyncio.wait_for(cancelled.wait(), timeout=5)
    assert cancelled.is_set()
