# -*- coding: utf-8 -*-
"""Administrator-only runtime diagnostics.

Exists because every capacity problem so far was diagnosed from a user's
symptom rather than from a number: "connection pool exhausted", "页面特别卡",
"导入卡住不动". This route answers "who is holding the shared resources right
now" — the thread pools and the platform connection pool — so the next
optimisation can be chosen from measurements instead of from a guess.

Admin-only via a router-level dependency (see ``_admin_guard``): a
diagnostics surface that leaks other tenants' load is itself a vulnerability,
and a route added later here cannot silently ship without the check.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ._admin_guard import require_admin_request

router = APIRouter(
    prefix="/debug",
    tags=["debug"],
    dependencies=[Depends(require_admin_request)],
)


@router.get("/io-pools")
async def get_io_pools() -> dict[str, Any]:
    """Thread-pool occupancy and the platform connection pool.

    ``active`` above ``max_workers`` means work is queued; ``waiting`` counts
    callers blocked on a slot, and ``busy_rejections`` counts the ones that
    gave up (they get a readable ``ThreadPoolBusy`` instead of an
    unattributed timeout).
    """
    from ...utils.io_pools import io_pool_stats, pool_wait_seconds

    platform: dict[str, Any]
    try:
        from ...platform_store.pool import pool_stats

        platform = pool_stats()
    except Exception:  # noqa: BLE001 - diagnostics must never 500
        platform = {"error": "unavailable"}

    return {
        "thread_pools": io_pool_stats(),
        "wait_budget_seconds": pool_wait_seconds(),
        "platform_db_pool": platform,
    }
