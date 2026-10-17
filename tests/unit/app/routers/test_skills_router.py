# -*- coding: utf-8 -*-
"""Unit tests for ``qwenpaw.app.routers.skills``.

The offline edition deliberately disables the Skill Hub (it is an
external service): these tests pin that behavior instead of the old
live-hub wiring —

- ``GET /skills`` — list workspace skills
- ``GET /skills/hub/search`` — always empty in offline mode
- ``GET /skills/pool`` — pool listing
- Hub install lifecycle: ``start`` always 400, ``status``/``cancel`` 404
  because no task can ever be created offline

Note: ``GET /skills`` still goes through ``get_agent_for_request``; it is
patched here, the hub-heavy mocks of the past are gone.
"""
# pylint: disable=protected-access,redefined-outer-name,unused-argument
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from qwenpaw.app.routers import skills as skills_module
from qwenpaw.app.routers.skills import router as skills_router


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    application.state.multi_agent_manager = MagicMock(name="ManagerStub")
    # ``skills_router`` already declares ``prefix="/skills"`` so we only
    # add the outer ``/api`` mount point used in production.
    application.include_router(skills_router, prefix="/api")
    return application


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def fake_workspace(tmp_path: Path):
    workspace = MagicMock(name="Workspace")
    workspace.workspace_dir = str(tmp_path)
    workspace.agent_id = "default"
    return workspace


@pytest.fixture
def patch_get_agent(fake_workspace):
    with patch(
        "qwenpaw.app.agent_context.get_agent_for_request",
        new=AsyncMock(return_value=fake_workspace),
    ) as patched:
        yield patched


# ---------------------------------------------------------------------------
# GET /skills
# ---------------------------------------------------------------------------


def test_list_skills_returns_workspace_specs(client, patch_get_agent):
    with patch(
        "qwenpaw.app.routers.skills._build_workspace_skill_specs",
        return_value=[],
    ) as build_mock:
        response = client.get("/api/skills")

    assert response.status_code == 200
    assert response.json() == []
    build_mock.assert_called_once()


def test_list_skills_404_when_agent_lookup_fails(client):
    from fastapi import HTTPException

    with patch(
        "qwenpaw.app.agent_context.get_agent_for_request",
        new=AsyncMock(
            side_effect=HTTPException(status_code=404, detail="missing"),
        ),
    ):
        response = client.get("/api/skills")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /skills/hub/search — offline: the hub is unreachable by design
# ---------------------------------------------------------------------------


def test_search_hub_always_empty_offline(client):
    """Skill Hub is an external service; offline deployment answers
    with an empty list regardless of the query."""
    response = client.get("/api/skills/hub/search?q=cool&limit=5")

    assert response.status_code == 200
    assert response.json() == []


# ---------------------------------------------------------------------------
# GET /skills/pool
# ---------------------------------------------------------------------------


def test_list_pool_skills_returns_pool_specs(client):
    with patch(
        "qwenpaw.app.routers.skills._build_pool_skill_specs",
        return_value=[],
    ) as build_mock:
        response = client.get("/api/skills/pool")

    assert response.status_code == 200
    assert response.json() == []
    build_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Hub install lifecycle — offline: never starts, never has tasks
# ---------------------------------------------------------------------------


def test_start_install_rejected_offline(client):
    """POST /hub/install/start is a hard 400 in offline deployments."""
    response = client.post(
        "/api/skills/hub/install/start",
        json={"bundle_url": "https://hub/example.zip"},
    )

    assert response.status_code == 400
    assert "Offline deployment" in response.json()["detail"]
    # No install task was ever registered.
    assert not skills_module._hub_install_tasks


def test_get_install_status_offline_no_tasks(client):
    """Offline mode cannot create install tasks, so any status lookup
    referees to 404."""
    response = client.get("/api/skills/hub/install/status/any-task-id")

    assert response.status_code == 404


def test_get_install_status_404_for_unknown_task(client):
    response = client.get("/api/skills/hub/install/status/no-such-task")

    assert response.status_code == 404


def test_cancel_install_offline_no_tasks(client):
    """Offline mode cannot create install tasks, so any cancel request
    referees to 404."""
    response = client.post("/api/skills/hub/install/cancel/any-task-id")

    assert response.status_code == 404


def test_cancel_install_404_for_unknown_task(client):
    response = client.post("/api/skills/hub/install/cancel/no-such-task")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Validation: missing bundle_url returns 422
# ---------------------------------------------------------------------------


def test_start_install_422_on_missing_bundle_url(client, patch_get_agent):
    response = client.post(
        "/api/skills/hub/install/start",
        json={"version": "1.0"},
    )

    assert response.status_code == 422
