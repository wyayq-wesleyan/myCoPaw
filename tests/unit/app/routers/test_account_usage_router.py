"""The per-account usage endpoint is administrator-only.

It exposes every account's consumption, so the request must be rejected
before any aggregation happens when the caller is not an administrator.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from qwenpaw.app.routers import token_usage as router_module


def _request(token: str | None = "Bearer alice-token"):
    headers = {"Authorization": token} if token else {}
    return SimpleNamespace(headers=headers)


def _call(request):
    """Invoke the endpoint with explicit args.

    Calling it directly (not through FastAPI) would otherwise pass the
    ``Query(...)`` default objects as real values, which muddies the failure
    mode when the guard under test is missing.
    """
    return router_module.get_account_usage(
        request=request,
        start_date="2026-09-01",
        end_date="2026-09-18",
        accounts=None,
    )


async def test_account_usage_rejects_an_unauthenticated_caller(monkeypatch):
    monkeypatch.setattr(router_module, "get_request_username", lambda _r: None)

    with pytest.raises(HTTPException) as excinfo:
        await _call(_request(None))

    assert excinfo.value.status_code == 401


async def test_account_usage_rejects_a_non_admin_account(monkeypatch):
    monkeypatch.setattr(router_module, "get_request_username", lambda _r: "alice")

    def _deny(username):
        raise HTTPException(status_code=403, detail="Administrator access required")

    monkeypatch.setattr(router_module, "require_admin_access", _deny)

    with pytest.raises(HTTPException) as excinfo:
        await _call(_request())

    assert excinfo.value.status_code == 403


async def test_account_usage_aggregates_for_an_administrator(monkeypatch):
    captured: dict = {}

    monkeypatch.setattr(router_module, "get_request_username", lambda _r: "admin")
    monkeypatch.setattr(router_module, "require_admin_access", lambda _u: None)

    class _Manager:
        async def get_account_usage(self, **kwargs):
            captured.update(kwargs)
            return "summary"

    monkeypatch.setattr(
        router_module,
        "get_token_usage_manager",
        lambda: _Manager(),
    )

    result = await router_module.get_account_usage(
        request=_request(),
        start_date="2026-09-01",
        end_date="2026-09-18",
        accounts="alice, bob",
    )

    assert result == "summary"
    assert captured["users"] == ["alice", "bob"]
    assert captured["start_date"].isoformat() == "2026-09-01"
    assert captured["end_date"].isoformat() == "2026-09-18"


async def test_account_usage_requests_every_account_by_default(monkeypatch):
    captured: dict = {}

    monkeypatch.setattr(router_module, "get_request_username", lambda _r: "admin")
    monkeypatch.setattr(router_module, "require_admin_access", lambda _u: None)

    class _Manager:
        async def get_account_usage(self, **kwargs):
            captured.update(kwargs)
            return "summary"

    monkeypatch.setattr(
        router_module,
        "get_token_usage_manager",
        lambda: _Manager(),
    )

    await router_module.get_account_usage(
        request=_request(),
        start_date="2026-09-01",
        end_date="2026-09-18",
        accounts=None,
    )

    # None means "no allowlist" so the manager returns every account.
    assert captured["users"] is None


async def test_account_usage_swaps_a_reversed_date_range(monkeypatch):
    captured: dict = {}

    monkeypatch.setattr(router_module, "get_request_username", lambda _r: "admin")
    monkeypatch.setattr(router_module, "require_admin_access", lambda _u: None)

    class _Manager:
        async def get_account_usage(self, **kwargs):
            captured.update(kwargs)
            return "summary"

    monkeypatch.setattr(
        router_module,
        "get_token_usage_manager",
        lambda: _Manager(),
    )

    await router_module.get_account_usage(
        request=_request(),
        start_date="2026-09-18",
        end_date="2026-09-01",
        accounts=None,
    )

    assert captured["start_date"].isoformat() == "2026-09-01"
    assert captured["end_date"].isoformat() == "2026-09-18"
