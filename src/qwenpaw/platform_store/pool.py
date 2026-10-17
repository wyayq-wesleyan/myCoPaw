# -*- coding: utf-8 -*-
"""Connection pool for the platform's PostgreSQL store.

Two properties matter more than throughput here:

* **The platform must start without PostgreSQL.** Databases are an external
  dependency; a deployment whose database is still coming up, or is briefly
  down, should boot and report the affected features as unavailable instead of
  crash-looping. ``is_available()`` therefore answers without raising, and
  callers that need data raise a typed error a route can turn into a clear
  message.

* **Connections are a finite, shared resource.** The pool is deliberately small
  and bounded, and acquisition waits briefly rather than failing instantly, so a
  burst of concurrent requests becomes latency instead of a confusing error.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import psycopg2
import psycopg2.extras
from psycopg2.pool import PoolError, ThreadedConnectionPool

from .config import load_platform_db_config

logger = logging.getLogger(__name__)

# Small on purpose: every platform query is short, and PostgreSQL's connection
# budget is shared with the knowledge service and the per-account analysis
# databases. Concurrency comes from short checkouts, not from a wide pool.
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 8

# How long a caller waits for a free slot before giving up.
POOL_WAIT_SECONDS = 15.0

# Generous session ceiling: nothing should hang forever, but a first-time index
# build over an existing history must not be cut off mid-way.
STATEMENT_TIMEOUT_MS = 60000

# Observability counters. Plain integers: bounded by construction and cheap to
# bump on every checkout. Without them a saturated pool is invisible until a
# user reports a timeout — and there is no way to tell "who is holding the
# connections" from the outside.
_wait_count = 0
_exhausted_count = 0
_peak_in_use = 0
_counters_lock = threading.Lock()

_pool: ThreadedConnectionPool | None = None
# Re-entrant on purpose. ``get_conn``'s wait branch re-resolves the pool while
# holding this lock (the pool it was waiting on may have been discarded), and
# ``_get_pool`` acquires the same lock when it has to reopen. With a plain
# ``Lock`` the thread would block on itself forever *while holding the lock*,
# freezing every later platform query too — verified by reproducing the hang.
_pool_lock = threading.RLock()
# Set once a connection attempt has failed, so health checks and log noise stay
# proportionate instead of retrying on every request.
_last_error: str | None = None


class PlatformStoreUnavailable(RuntimeError):
    """The platform database could not be reached.

    Raised instead of a raw driver error so callers can distinguish "the
    database is unavailable" (an operational state the UI should explain) from a
    genuine query bug (which should surface as a failure).
    """

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__(
            "平台数据库（PostgreSQL）当前不可用"
            + (f"：{detail}" if detail else "")
            + "。请确认数据库服务已启动，且连接配置正确。",
        )


def _open_pool() -> ThreadedConnectionPool:
    """Open the pool, trying each candidate host once."""
    config = load_platform_db_config()
    last_exc: Exception | None = None
    for host in config.host_candidates:
        try:
            pool = ThreadedConnectionPool(
                POOL_MIN_SIZE,
                POOL_MAX_SIZE,
                **{
                    **config.connect_kwargs(host),
                    "options": f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
                },
            )
            logger.info(
                "platform store: connected to %s:%s/%s",
                host,
                config.port,
                config.dbname,
            )
            return pool
        except Exception as exc:  # noqa: BLE001 - try the next candidate host
            last_exc = exc
            logger.debug(
                "platform store: connection to %s failed", host, exc_info=True,
            )
    raise PlatformStoreUnavailable(str(last_exc) if last_exc else "unknown error")


def _get_pool() -> ThreadedConnectionPool:
    """Return the pool, opening it on first use.

    Raises ``PlatformStoreUnavailable`` when no candidate host accepts a
    connection. The failed attempt is not cached: the database may come up
    later, and the next caller should be able to succeed.
    """
    global _pool, _last_error
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        try:
            _pool = _open_pool()
        except PlatformStoreUnavailable as exc:
            _last_error = exc.detail
            raise
        _last_error = None
        return _pool


def get_conn():
    """Check out a connection, waiting briefly when every slot is taken.

    ``ThreadedConnectionPool.getconn`` raises the instant the pool is full;
    waiting turns a short burst into ordinary latency. The wait is bounded so a
    genuinely stuck pool still fails rather than pinning a worker thread.
    """
    global _pool, _wait_count, _exhausted_count, _peak_in_use
    pool = _get_pool()
    deadline = time.monotonic() + POOL_WAIT_SECONDS
    delay = 0.02
    waited = False
    while True:
        try:
            conn = pool.getconn()
        except PoolError:
            if not waited:
                waited = True
                with _counters_lock:
                    _wait_count += 1
            if time.monotonic() >= deadline:
                with _counters_lock:
                    _exhausted_count += 1
                raise PlatformStoreUnavailable("连接池已耗尽") from None
            time.sleep(delay)
            delay = min(delay * 2, 0.25)
            # A holder may have discarded the pool on error; re-resolve so the
            # wait cannot spin against a dead object.
            with _pool_lock:
                if _pool is not pool:
                    pool = _get_pool()
            continue
        with _counters_lock:
            in_use = len(getattr(pool, "_used", {}) or {})
            if in_use > _peak_in_use:
                _peak_in_use = in_use
        return conn


def put_conn(conn) -> None:
    """Return a connection to the pool, discarding it if it is broken."""
    global _pool
    with _pool_lock:
        pool = _pool
    if pool is None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        pool.putconn(conn)
    except Exception:  # noqa: BLE001 - never let bookkeeping raise
        logger.debug("platform store: could not return a connection", exc_info=True)


def is_available() -> bool:
    """Whether the platform database can be reached right now.

    Never raises: callers use it for health reporting and feature gating.
    """
    try:
        conn = get_conn()
    except PlatformStoreUnavailable:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        put_conn(conn)


def pool_stats() -> dict[str, Any]:
    """Snapshot of the platform pool. Never raises, never blocks.

    Read-only on purpose: the debug endpoint must be safe to call while the
    pool is saturated (that is exactly when someone will call it).
    """
    try:
        with _pool_lock:
            pool = _pool
        with _counters_lock:
            observed = {
                "wait_count": _wait_count,
                "exhausted_count": _exhausted_count,
                "peak_in_use": _peak_in_use,
            }
        if pool is None:
            return {
                "max_size": POOL_MAX_SIZE,
                "in_use": 0,
                "idle": 0,
                "wait_seconds": POOL_WAIT_SECONDS,
                "open": False,
                **observed,
            }
        used = getattr(pool, "_used", {}) or {}
        idle = getattr(pool, "_pool", []) or []
        return {
            "max_size": POOL_MAX_SIZE,
            "in_use": len(used),
            "idle": len(idle),
            "wait_seconds": POOL_WAIT_SECONDS,
            "open": True,
            **observed,
        }
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        logger.debug("platform store: pool stats unavailable", exc_info=True)
        return {"error": "unavailable"}


def last_error() -> str | None:
    """The most recent connection failure detail, for diagnostics."""
    return _last_error


def reset_pool() -> None:
    """Close and forget the pool (tests, and after a configuration change)."""
    global _pool, _last_error
    with _pool_lock:
        pool, _pool = _pool, None
        _last_error = None
    if pool is not None:
        try:
            pool.closeall()
        except Exception:  # noqa: BLE001
            logger.debug("platform store: pool close failed", exc_info=True)


def dict_cursor(conn) -> Any:
    """A cursor returning rows as dicts (the shape the history code expects)."""
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


__all__ = [
    "POOL_MAX_SIZE",
    "POOL_MIN_SIZE",
    "PlatformStoreUnavailable",
    "dict_cursor",
    "get_conn",
    "is_available",
    "last_error",
    "pool_stats",
    "put_conn",
    "reset_pool",
]
