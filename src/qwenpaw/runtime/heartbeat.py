# -*- coding: utf-8 -*-
"""SSE heartbeat wrapper for async iterators."""
from __future__ import annotations

import asyncio

HEARTBEAT_INTERVAL_SECONDS = 25.0
_HEARTBEAT_TICK = object()


async def _iter_with_heartbeat(source_iter, interval: float):
    """Wrap an async-iter so it yields ``_HEARTBEAT_TICK`` on idle.

    The tick means "the wrapped iterator has produced nothing for *interval*
    seconds", which is what keeps a long approval wait or a slow model call
    from dropping the SSE connection.

    ``asyncio.wait`` is used rather than ``asyncio.wait_for`` on purpose. They
    are not equivalent here: ``asyncio.TimeoutError`` *is* ``TimeoutError``
    (since 3.11), and this project's own ``StreamIdleTimeoutError`` /
    ``StreamCleanupPendingError`` subclass it. Awaiting the inner task through
    ``wait_for`` therefore cannot tell "my interval elapsed" apart from "the
    iterator raised a timeout of its own", and a ``StreamIdleTimeoutError``
    from the model layer was swallowed as a heartbeat. Because a failed task
    was not cleared, the next iteration awaited that same already-failed task
    and re-raised instantly — an unbounded tight loop emitting ~2500 heartbeat
    events per second while the run never advanced.

    With ``asyncio.wait`` the timeout is reported by the *wait* (``done`` is
    empty) instead of by an exception, so the wrapped iterator's real
    exceptions propagate to the caller and the run fails visibly.
    """
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(source_iter.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                # The iterator is still working; nothing to report yet.
                yield _HEARTBEAT_TICK
                continue
            # Clear before use: a completed task must never be awaited twice.
            finished, pending = pending, None
            try:
                value = finished.result()
            except StopAsyncIteration:
                return
            yield value
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
