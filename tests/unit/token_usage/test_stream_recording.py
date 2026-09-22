# -*- coding: utf-8 -*-
"""Recording must survive a consumer that stops at the final chunk.

``TokenRecordingModelWrapper`` wraps every provider model, and
``RetryChatModel`` wraps that wrapper. ``RetryChatModel`` returns as soon as it
sees the chunk flagged ``is_last`` and closes the inner generator; the wrapper's
usage bookkeeping used to sit *after* its ``async for`` loop, so a
normally-finished stream never reached it and no usage was recorded at all.

That is a silent data-loss bug, so it is pinned here at the generator level
rather than only through a full app boot: a stream consumed until the final
chunk — the way the retry layer consumes it — must still be recorded exactly
once.
"""
from __future__ import annotations

from typing import AsyncGenerator

import pytest

from qwenpaw.token_usage.manager import TokenUsageManager
from qwenpaw.token_usage.model_wrapper import TokenRecordingModelWrapper


@pytest.fixture(autouse=True)
def _isolate_token_usage_manager():
    """Reset the process-wide manager so each test starts empty."""
    # pylint: disable=protected-access
    TokenUsageManager._instance = None
    yield
    TokenUsageManager._instance = None


class _Usage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Chunk:
    def __init__(self, *, usage=None, is_last: bool = False) -> None:
        self.usage = usage
        self.is_last = is_last


def _wrapper() -> TokenRecordingModelWrapper:
    """A recording wrapper around an unused inner model.

    ``_record_usage`` only reads ``self.model`` and ``self._provider_id`` when an
    event is emitted, so the inner model is never touched by these tests.
    """
    return TokenRecordingModelWrapper("prov", object())  # type: ignore[arg-type]


async def _stream(chunks: list[_Chunk]) -> AsyncGenerator[_Chunk, None]:
    for chunk in chunks:
        yield chunk


async def _drain(stream) -> int:
    seen = 0
    async for _ in stream:
        seen += 1
    return seen


async def test_usage_is_recorded_when_consumer_stops_at_last_chunk() -> None:
    """Stopping at ``is_last`` (as RetryChatModel does) must still record.

    ``RetryChatModel`` ``return``s from its own loop on the ``is_last`` chunk
    and then closes the wrapped generator in a ``finally`` (via
    ``_close_stream_bounded`` → ``aclose()``). The explicit ``aclose()`` below
    reproduces that: a bare ``break`` would leave the generator suspended and
    defer the check to garbage collection, which is not what production does.
    """
    wrapper = _wrapper()
    recorded: list = []
    wrapper._record_usage = recorded.append  # type: ignore[assignment]

    chunks = [
        _Chunk(),
        _Chunk(usage=_Usage(10, 5), is_last=True),
    ]
    stream = wrapper._wrap_stream(_stream(chunks))
    seen = 0
    async for chunk in stream:
        seen += 1
        if chunk.is_last:
            break
    await stream.aclose()

    assert seen == 2
    assert len(recorded) == 1, "usage lost when the consumer stopped early"
    assert recorded[0].input_tokens == 10
    assert recorded[0].output_tokens == 5


async def test_full_drain_records_exactly_once() -> None:
    """A fully consumed stream must not double-count."""
    wrapper = _wrapper()
    recorded: list = []
    wrapper._record_usage = recorded.append  # type: ignore[assignment]

    chunks = [_Chunk(), _Chunk(usage=_Usage(7, 3), is_last=True)]
    seen = await _drain(wrapper._wrap_stream(_stream(chunks)))

    assert seen == 2
    assert len(recorded) == 1
    assert recorded[0].input_tokens == 7
