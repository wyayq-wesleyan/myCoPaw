# -*- coding: utf-8 -*-
"""Unit tests for the provider OAuth router: registration and access control.

Starting a flow writes the exchanged credential into the *global* provider
configuration, so ``/oauth/start`` and ``/oauth/status`` are administrator-only.
The callback is a browser redirect target (no ``Authorization`` header) and is
deliberately left reachable; its safety rests on the unguessable ``state`` and
on ``start`` being admin-only.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from qwenpaw.app.routers import _admin_guard
from qwenpaw.app.routers import router as api_router


@pytest.fixture
def client() -> TestClient:
    """App mounting the real API router (real guard dependency, not a stub).

    ``raise_server_exceptions=False`` keeps handler errors as HTTP responses so
    a missing dependency surfaces as a status code instead of a traceback.
    """
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def as_admin(monkeypatch):
    """Make the admin guard accept the request without a real auth store."""

    def _inject(request: Request) -> str | None:
        request.state.user = "admin"
        return "admin"

    monkeypatch.setattr(_admin_guard, "get_request_username", _inject)
    monkeypatch.setattr(_admin_guard, "require_admin_access", lambda _u: None)


def test_oauth_start_refuses_an_anonymous_caller(client) -> None:
    response = client.post("/api/providers/openrouter/oauth/start")

    assert response.status_code == 401


def test_oauth_status_refuses_an_anonymous_caller(client) -> None:
    response = client.get(
        "/api/providers/openrouter/oauth/status",
        params={"state": "x"},
    )

    assert response.status_code == 401


def test_openrouter_oauth_start_is_registered_for_an_admin(client, as_admin) -> None:
    """The route must exist (not 405) and return a usable authorize URL."""
    response = client.post("/api/providers/openrouter/oauth/start")

    assert response.status_code == 200
    payload = response.json()
    assert payload["flow_type"] == "browser_redirect"
    assert payload["authorize_url"].startswith("https://openrouter.ai/auth")
    assert payload["state"]


def test_oauth_callback_is_not_guarded(client) -> None:
    """No auth header is available on a redirect, so the route must run.

    The assertion is only that the admin guard did not reject it (not 401);
    the handler itself needs ``app.state.provider_manager``, which this
    synthetic app does not build.
    """
    response = client.get(
        "/api/providers/openrouter/oauth/callback",
        params={"code": "x"},
    )

    assert response.status_code != 401
