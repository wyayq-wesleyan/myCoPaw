# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name
"""Unit tests for the global settings router (/api/settings/language)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from qwenpaw.app.routers.settings import router

app = FastAPI()
app.include_router(router, prefix="/api")


@pytest.fixture(autouse=True)
def _use_tmp_settings(tmp_path: Path):
    """Redirect settings file to a temp directory for every test."""
    settings_file = tmp_path / "settings.json"
    with patch("qwenpaw.app.routers.settings._SETTINGS_FILE", settings_file):
        yield settings_file


@pytest.fixture
def api_client():
    """Create an async test client."""
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ── GET /settings/language ───────────────────────────────────────────


async def test_get_language_default(api_client):
    """The air-gapped console has one supported language: zh."""
    async with api_client:
        resp = await api_client.get("/api/settings/language")
    assert resp.status_code == 200
    assert resp.json() == {"language": "zh"}


async def test_get_language_ignores_persisted_value(
    api_client,
    _use_tmp_settings,
):
    """GET always reports zh even if an old settings file holds another value."""
    _use_tmp_settings.write_text(json.dumps({"language": "ja"}), "utf-8")
    async with api_client:
        resp = await api_client.get("/api/settings/language")
    assert resp.status_code == 200
    assert resp.json() == {"language": "zh"}


# ── PUT /settings/language ───────────────────────────────────────────


@pytest.mark.parametrize("lang", ["en", "zh", "ja", "ru", "pt-BR", "id"])
async def test_put_language_valid(
    api_client,
    lang,
    _use_tmp_settings,
):
    """PUT responds, but the console language stays pinned to zh."""
    async with api_client:
        resp = await api_client.put(
            "/api/settings/language",
            json={"language": lang},
        )
    assert resp.status_code == 200
    assert resp.json() == {"language": "zh"}

    # The requested language is not persisted anymore.
    assert not _use_tmp_settings.exists()


@pytest.mark.parametrize(
    "payload",
    [{"language": "xx"}, {"language": ""}, {"lang": "zh"}],
)
async def test_put_language_ignores_unsupported_body(api_client, payload):
    """Any body is accepted and the language stays zh."""
    async with api_client:
        resp = await api_client.put("/api/settings/language", json=payload)
    assert resp.status_code == 200
    assert resp.json() == {"language": "zh"}


async def test_put_then_get_roundtrip(api_client):
    """PUT then GET always reports the pinned language."""
    async with api_client:
        await api_client.put(
            "/api/settings/language",
            json={"language": "ru"},
        )
        resp = await api_client.get("/api/settings/language")
    assert resp.json() == {"language": "zh"}


async def test_put_language_preserves_other_settings(
    api_client,
    _use_tmp_settings,
):
    """PUT must not overwrite other keys in settings.json."""
    _use_tmp_settings.write_text(
        json.dumps({"theme": "dark", "language": "en"}),
        "utf-8",
    )
    async with api_client:
        resp = await api_client.put(
            "/api/settings/language",
            json={"language": "zh"},
        )
    assert resp.json() == {"language": "zh"}

    data = json.loads(_use_tmp_settings.read_text("utf-8"))
    assert data == {"theme": "dark", "language": "en"}


@pytest.fixture
def as_admin(monkeypatch):
    """Let the admin guard accept the request without a real auth store."""

    from qwenpaw.app.routers import _admin_guard

    monkeypatch.setattr(
        _admin_guard,
        "get_request_username",
        lambda _request: "admin",
    )
    monkeypatch.setattr(_admin_guard, "require_admin_access", lambda _u: None)


async def test_offload_policy_requires_admin(api_client):
    """Unauthenticated PUT to the admin-only policy is rejected."""
    async with api_client:
        resp = await api_client.put(
            "/api/settings/offload-policy",
            json={"default_action": "offload"},
        )
    assert resp.status_code == 401


async def test_concurrent_language_and_offload_policy_updates(
    api_client,
    as_admin,
    _use_tmp_settings,
):
    """Concurrent PUTs must not drop either key (path lock + atomic write)."""
    import asyncio

    async with api_client:
        await asyncio.gather(
            api_client.put(
                "/api/settings/language",
                json={"language": "zh"},
            ),
            api_client.put(
                "/api/settings/offload-policy",
                json={"default_action": "offload"},
            ),
        )
        lang = await api_client.get("/api/settings/language")
        policy = await api_client.get("/api/settings/offload-policy")

    assert lang.json() == {"language": "zh"}
    assert policy.json() == {"default_action": "offload"}
    data = json.loads(_use_tmp_settings.read_text("utf-8"))
    # The language PUT no longer persists anything.
    assert data["offload_policy"] == "offload"
