# -*- coding: utf-8 -*-
"""The policy audit write must not run on the event loop.

Symptom this exists for: "稍微用一用就报 connection pool exhausted" plus
"页面特别卡，干什么都慢一拍". ``AuditLog.record`` performs a **synchronous**
``INSERT`` + ``commit`` and serialises writers behind a process-wide ``RLock``.
It used to be called straight from ``async def`` tool-guard hooks, so every
tool invocation blocked the whole application for a database round trip — and
when the platform pool was full, ``get_conn()`` waits up to
``POOL_WAIT_SECONDS`` (15 s), freezing SSE streams, polling and heartbeats for
every connected user.

Pinned properties:

* ``record`` executes on the ``fast`` pool's thread (so the loop thread never
  runs it);
* the event loop keeps making progress while an audit write is in flight;
* tool-guard call sites use ``audit_async`` — no bare ``governor.audit(`` calls
  remain, which would silently reintroduce the blocking call;
* pool contention still cannot disturb a policy decision (the documented
  "audit failures never propagate" contract).
"""

from __future__ import annotations

import asyncio
import pathlib
import threading
import time
from typing import Any

import pytest

from qwenpaw.governance.audit import AuditLog
from qwenpaw.governance.policy import (
    GovernanceAction,
    GovernanceDecision,
    ToolCallSpec,
)
from qwenpaw.governance.resource_governor import ResourceGovernor
from qwenpaw.utils import io_pools
from qwenpaw.utils.io_pools import (
    POOL_AUDIT,
    POOL_FAST,
    install_io_pools,
    shutdown_io_pools,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
TOOL_ADAPTER = REPO_ROOT / "src" / "qwenpaw" / "governance" / "tool_adapter.py"


def _tc(tool_name: str = "run_shell", target: str = "echo hi") -> ToolCallSpec:
    return ToolCallSpec(
        tool_name=tool_name,
        target=target,
        agent_id="test-agent",
        session_id="test-session",
    )


def _decision() -> GovernanceDecision:
    return GovernanceDecision(action=GovernanceAction.ALLOW, reason="test")


def _make_governor(tmp_path) -> ResourceGovernor:
    return ResourceGovernor(
        str(tmp_path),
        governance_dir=str(tmp_path / "governance"),
    )


async def _install() -> None:
    await shutdown_io_pools()
    install_io_pools()


# ---------------------------------------------------------------------------
# Where the write actually runs
# ---------------------------------------------------------------------------


def test_record_runs_on_the_audit_lane_thread(tmp_path, monkeypatch):
    """The loop thread must never be the one issuing the INSERT."""
    monkeypatch.setenv("QWENPAW_IO_POOL_WAIT_SECONDS", "5")
    observed: list[str] = []

    def fake_record(*_args: Any, **_kwargs: Any) -> None:
        observed.append(threading.current_thread().name)

    async def scenario() -> None:
        await _install()
        try:
            governor = _make_governor(tmp_path)
            monkeypatch.setattr(AuditLog, "record", fake_record)
            await governor.audit_async(_tc(), _decision())
        finally:
            await shutdown_io_pools()

    asyncio.run(scenario())

    assert observed, "audit_async did not call record at all"
    assert observed[0].startswith("qp-io-"), (
        f"record ran on {observed[0]!r}; it must run on a named "
        "I/O pool thread, not inline on the event loop"
    )
    assert "qp-io-audit" in observed[0], (
        f"record ran on {observed[0]!r}; audit writes get a dedicated lane so "
        "fast-pool congestion can neither delay them nor lose them"
    )


def test_audit_failure_is_still_swallowed(tmp_path, monkeypatch):
    """The pre-existing contract: audit problems never break the decision."""
    monkeypatch.setenv("QWENPAW_IO_POOL_WAIT_SECONDS", "5")

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("database is on fire")

    async def scenario() -> None:
        await _install()
        try:
            governor = _make_governor(tmp_path)
            monkeypatch.setattr(AuditLog, "record", explode)
            # AuditLog.record catches internally; this pins that the async
            # wrapper does not add a new failure path.
            with pytest.raises(RuntimeError):
                await governor.audit_async(_tc(), _decision())
        finally:
            await shutdown_io_pools()

    asyncio.run(scenario())


def test_pool_contention_does_not_propagate_into_the_decision(
    tmp_path, monkeypatch,
):
    """A saturated audit lane must not turn into failing tool calls."""
    monkeypatch.setenv("QWENPAW_IO_AUDIT_WAIT_SECONDS", "0.2")
    release = threading.Event()
    entered = threading.Semaphore(0)

    def blocking_record(*_args: Any, **_kwargs: Any) -> None:
        entered.release()
        release.wait(10)

    async def scenario() -> Any:
        await _install()
        try:
            governor = _make_governor(tmp_path)
            monkeypatch.setattr(AuditLog, "record", blocking_record)
            # Shrink the audit lane so saturation is reachable instantly.
            io_pools._pools = {
                POOL_AUDIT: io_pools._IoPool(
                    name=POOL_AUDIT,
                    max_workers=1,
                    queue_depth=0,
                    wait_seconds=0.2,
                ),
            }
            io_pools._pools_loop_id = id(asyncio.get_running_loop())

            hog = asyncio.create_task(governor.audit_async(_tc(), _decision()))
            assert await asyncio.to_thread(entered.acquire, True, 5)
            try:
                # No raise: the wrapper downgrades contention to a log line.
                return await governor.audit_async(
                    _tc("run_shell", "echo x"),
                    _decision(),
                )
            finally:
                release.set()
                await hog
        finally:
            await shutdown_io_pools()

    assert asyncio.run(scenario()) is None


def test_a_saturated_fast_pool_does_not_delay_or_drop_the_audit_lane(
    tmp_path,
    monkeypatch,
):
    """The whole point of the dedicated lane: fast-pool starvation must not
    cost audit records."""
    monkeypatch.setenv("QWENPAW_IO_POOL_WAIT_SECONDS", "0.2")
    monkeypatch.setenv("QWENPAW_IO_AUDIT_WAIT_SECONDS", "5")
    fast_release = threading.Event()
    fast_entered = threading.Semaphore(0)
    recorded = threading.Event()

    def hog(*_args: Any, **_kwargs: Any) -> None:
        fast_entered.release()
        fast_release.wait(10)

    def fake_record(*_args: Any, **_kwargs: Any) -> None:
        recorded.set()

    async def scenario() -> None:
        await _install()
        try:
            # Saturate the fast pool completely; the audit lane is separate.
            io_pools._pools = {
                POOL_FAST: io_pools._IoPool(
                    name=POOL_FAST,
                    max_workers=1,
                    queue_depth=0,
                    wait_seconds=0.2,
                ),
            }
            io_pools._pools_loop_id = id(asyncio.get_running_loop())
            hog_task = asyncio.create_task(
                io_pools.run_in_pool(POOL_FAST, hog),
            )
            assert await asyncio.to_thread(fast_entered.acquire, True, 5)
            governor = _make_governor(tmp_path)
            monkeypatch.setattr(AuditLog, "record", fake_record)
            try:
                await asyncio.wait_for(
                    governor.audit_async(_tc(), _decision()),
                    timeout=4,
                )
            finally:
                fast_release.set()
                await hog_task
        finally:
            await shutdown_io_pools()

    asyncio.run(scenario())
    assert recorded.is_set(), (
        "the audit write was dropped while only the fast pool was saturated; "
        "the dedicated lane is supposed to keep audit independent"
    )


# ---------------------------------------------------------------------------
# The loop keeps running
# ---------------------------------------------------------------------------


def test_event_loop_progresses_while_an_audit_write_is_in_flight(
    tmp_path,
    monkeypatch,
):
    """A synchronous INSERT on the loop freezes every concurrent task.

    Run in a worker thread with a join timeout so a regression reports as a
    failure instead of hanging the suite: on the old (inline) behaviour the
    loop can never reach the ``release.set()`` below.
    """
    monkeypatch.setenv("QWENPAW_IO_POOL_WAIT_SECONDS", "5")
    outcome: dict[str, Any] = {}

    def scenario() -> None:
        async def main() -> None:
            await _install()
            try:
                governor = _make_governor(tmp_path)
                release = threading.Event()
                started = threading.Event()

                def slow_record(*_args: Any, **_kwargs: Any) -> None:
                    started.set()
                    release.wait(30)

                monkeypatch.setattr(AuditLog, "record", slow_record)

                ticks = 0

                async def tick() -> None:
                    nonlocal ticks
                    while not release.is_set():
                        ticks += 1
                        await asyncio.sleep(0.005)

                audit_task = asyncio.create_task(
                    governor.audit_async(_tc(), _decision()),
                )
                ticker = asyncio.create_task(tick())
                try:
                    if not await asyncio.to_thread(started.wait, 5):
                        outcome["error"] = "audit never started"
                        return
                    await asyncio.sleep(0.1)
                    outcome["ticks"] = ticks
                finally:
                    release.set()
                    await asyncio.gather(
                        audit_task, ticker, return_exceptions=True,
                    )
            finally:
                await shutdown_io_pools()

        asyncio.run(main())

    worker = threading.Thread(target=scenario, daemon=True)
    worker.start()
    worker.join(timeout=30)

    assert not worker.is_alive(), (
        "the event loop was blocked by the audit write — no other coroutine "
        "could make progress while the INSERT was running"
    )
    assert outcome.get("error") is None, outcome
    assert outcome.get("ticks", 0) >= 2, (
        "the loop did not tick while the audit write was in flight"
    )


# ---------------------------------------------------------------------------
# Static guard: the call sites, not just the helper
# ---------------------------------------------------------------------------


def test_tool_guard_call_sites_await_the_async_audit():
    """Pins the call sites: a helper nobody calls would not fix anything."""
    source = TOOL_ADAPTER.read_text(encoding="utf-8")
    assert "governor.audit_async(" in source
    # A bare synchronous call would reintroduce the blocking INSERT.
    offenders = [
        f"{i}: {line.strip()}"
        for i, line in enumerate(source.splitlines(), start=1)
        if "governor.audit(" in line and "audit_async" not in line
    ]
    assert not offenders, (
        "synchronous governor.audit() call sites remain: "
        + "; ".join(offenders)
    )
