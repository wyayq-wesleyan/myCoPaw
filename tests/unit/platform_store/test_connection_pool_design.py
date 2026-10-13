# -*- coding: utf-8 -*-
"""Connection-pool correctness: the failure modes that surface as "连接已满".

Three separate defects lived behind that one symptom. Each is pinned here so it
cannot come back silently.

1. **Self-deadlock on the platform pool.** ``get_conn`` waits for a free slot by
   re-resolving the pool *while holding* ``_pool_lock``, and ``_get_pool`` takes
   the same lock to (re)open it. With a non-reentrant ``threading.Lock`` the
   thread blocks on itself **while still holding the lock**, which freezes every
   later caller — a total outage, not a slow request. Reproduced before the fix.

2. **A wrong connection budget.** The graph layer's docstring/``.env`` said
   ``max_workspaces × pool_max_connections``, but LightRAG's ``ClientManager`` is
   a process-wide singleton, so the real figure is just
   ``pool_max_connections``. Overstating it ~32x is what an operator would size
   PostgreSQL's ``max_connections`` from.

3. **A pool smaller than its own concurrency ceiling.** Every knowledge store
   call arrives via ``asyncio.to_thread``, so the demand ceiling is the server's
   worker-thread count, not a user count. A pool below that queues and then fails
   with an opaque ``PoolTimeout``.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
POOL_PATH = REPO_ROOT / "src" / "qwenpaw" / "platform_store" / "pool.py"
GRAPH_PATH = REPO_ROOT / "knowledge-service" / "app" / "graph_store.py"
KS_STORE_PATH = REPO_ROOT / "knowledge-service" / "app" / "store.py"
KS_ENV_PATH = REPO_ROOT / "knowledge-service" / ".env.example"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1) The platform pool lock must be reentrant
# ---------------------------------------------------------------------------


def test_platform_pool_lock_is_reentrant():
    """A plain Lock self-deadlocks: the wait branch re-resolves under the lock.

    Asserted on the source because the deadlock is a property of the lock type;
    the behavioural test below then proves the reentrancy actually holds.
    """
    source = _src(POOL_PATH)

    assert "threading.RLock()" in source, (
        "the platform pool lock must be reentrant: get_conn re-resolves the pool "
        "while holding it, and _get_pool re-acquires it"
    )
    assert "_pool_lock = threading.Lock()" not in source


def test_wait_branch_still_reresolves_the_pool():
    """Guard the reason the lock must be reentrant — do not just delete the need."""
    source = _src(POOL_PATH)

    # Locate get_conn specifically: it is the function whose wait branch
    # re-resolves the pool under the lock.
    body = source[source.index("def get_conn(") :]
    body = body[: body.index("\ndef ", 1)]

    assert "with _pool_lock:" in body, "get_conn must still re-resolve under the lock"
    lock_idx = body.index("with _pool_lock:")
    assert "_get_pool()" in body[lock_idx : lock_idx + 400], (
        "the pool is re-resolved inside the lock — that is why it must be reentrant"
    )


def test_reresolving_the_pool_does_not_deadlock():
    """Drive the exact sequence: pool cleared, waiter holding the lock."""
    p = pytest.importorskip("qwenpaw.platform_store.pool")
    lock = p._pool_lock

    # Only the lock type is under test here; no database is needed.
    done: list[float] = []

    def waiter() -> None:
        t0 = time.monotonic()
        with lock:
            with lock:  # what _get_pool() does when it must reopen
                pass
        done.append(time.monotonic() - t0)

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    t.join(timeout=5)

    assert not t.is_alive(), "acquiring the pool lock twice on one thread hung"
    assert done and done[0] < 1.0


# ---------------------------------------------------------------------------
# 2) The graph connection budget must be stated correctly
# ---------------------------------------------------------------------------


def test_graph_docstring_does_not_multiply_connections_by_workspaces():
    """The 32x overstatement is what mislead capacity planning."""
    source = _src(GRAPH_PATH)

    assert "max_workspaces × pool_max_connections" not in source
    assert "max_workspaces x pool_max_connections" not in source


def test_graph_docstring_explains_the_single_shared_pool():
    source = _src(GRAPH_PATH)

    assert "singleton" in source.lower() or "process-wide" in source.lower()
    assert "max_workspaces" in source, "the memory-only role must still be named"


def test_env_example_does_not_claim_a_product():
    """``连接总量 ≈ 上限 × 池上限`` is the wrong formula."""
    source = _src(KS_ENV_PATH)

    assert "连接总量 ≈ 上限 × 池上限" not in source
    assert "KNOWLEDGE_GRAPH_POOL_MAX_CONNECTIONS" in source


# ---------------------------------------------------------------------------
# 3) The knowledge pool must not be smaller than its demand ceiling
# ---------------------------------------------------------------------------


def _int_const(source: str, name: str) -> int:
    match = re.search(rf"{name}\s*=\s*(\d+)", source)
    assert match, f"{name} not found"
    return int(match.group(1))


def test_knowledge_pool_is_large_enough_for_the_thread_ceiling():
    """anyio defaults to 40 worker threads; every store call needs a connection.

    A pool below that ceiling turns concurrency into queuing and then into
    PoolTimeout, which is exactly the reported symptom.
    """
    size = _int_const(_src(KS_STORE_PATH), "POOL_MAX_SIZE")

    assert size >= 30, (
        f"knowledge pool is {size}; it must be raised close to the worker-thread "
        "ceiling (40) or concurrent requests will exhaust it"
    )


def test_knowledge_pool_has_an_explicit_bounded_wait():
    """Without an explicit timeout the pool blocks on psycopg_pool's default and
    the failure reads as a hang rather than a saturation."""
    source = _src(KS_STORE_PATH)

    assert "POOL_WAIT_SECONDS" in source
    assert re.search(r"timeout=\s*self\.POOL_WAIT_SECONDS", source), (
        "ConnectionPool must be given an explicit timeout"
    )


def test_pool_exhaustion_raises_a_diagnostic_error():
    """The user-facing message must name saturation, not leak a bare PoolTimeout."""
    source = _src(KS_STORE_PATH)

    assert "PoolTimeout" in source
    assert "连接池已满" in source, "the raised error must explain the cause"


def test_pool_timeout_import_is_optional():
    """The pool is optional; importing the module must not require psycopg_pool."""
    source = _src(KS_STORE_PATH)

    assert "except ImportError" in source.split("class Store")[0], (
        "PoolTimeout must be imported defensively so the no-pool fallback works"
    )
