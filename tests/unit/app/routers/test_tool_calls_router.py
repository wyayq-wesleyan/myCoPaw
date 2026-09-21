# -*- coding: utf-8 -*-
"""Unit tests for ``qwenpaw.app.routers.tool_calls`` session scoping."""
# pylint: disable=redefined-outer-name
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from qwenpaw.app.routers.tool_calls import router as tool_calls_router
from qwenpaw.tool_calls._context import ToolCallContext
from qwenpaw.tool_calls._entry import ToolCallEntry, ToolCallStatus
from qwenpaw.tool_calls._stream import ToolStream


@pytest.fixture
def coordinator() -> MagicMock:
    return MagicMock(name="ToolCoordinator")


@pytest.fixture
def client(coordinator: MagicMock) -> TestClient:
    application = FastAPI()
    application.state.app_services = SimpleNamespace(
        tool_coordinator=coordinator,
    )
    application.include_router(tool_calls_router, prefix="/api")
    return TestClient(application)


def _running_entry(
    *,
    tool_call_id: str = "tc-1",
    session_id: str = "session-a",
) -> ToolCallEntry:
    return ToolCallEntry(
        ctx=ToolCallContext(
            tool_call_id=tool_call_id,
            tool_name="shell",
            session_id=session_id,
            agent_id="agent-1",
            root_session_id="root-1",
            started_at=0.0,
            offload_deadline=None,
            cancel_event=asyncio.Event(),
        ),
        stream=ToolStream(tool_call_id=tool_call_id, session_id=session_id),
        final_response=None,
        status=ToolCallStatus.RUNNING,
    )


def test_get_call_rejects_session_mismatch(
    client: TestClient,
    coordinator: MagicMock,
) -> None:
    coordinator.get.return_value = _running_entry(session_id="session-a")
    resp = client.get("/api/tool-calls/session-b/tc-1")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Tool call not found"


def test_cancel_rejects_session_mismatch(
    client: TestClient,
    coordinator: MagicMock,
) -> None:
    coordinator.get.return_value = _running_entry(session_id="session-a")
    resp = client.post("/api/tool-calls/wrong-session/tc-1/cancel")
    assert resp.status_code == 404
    coordinator.cancel.assert_not_called()


def test_get_call_allows_matching_session(
    client: TestClient,
    coordinator: MagicMock,
) -> None:
    coordinator.get.return_value = _running_entry(session_id="session-a")
    resp = client.get("/api/tool-calls/session-a/tc-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tool_call_id"] == "tc-1"
    assert body["session_id"] == "session-a"


# ---------------------------------------------------------------------------
# Account scoping
#
# A session id is not a credential — it appears in URLs and logs — so once auth
# is on, the caller must own the agent that ran the tool. Before this, knowing a
# session id was enough to read, cancel or extend another account's tool calls.
# ---------------------------------------------------------------------------


@pytest.fixture
def as_user(monkeypatch):
    """Turn auth on and fix the request identity to *name*."""

    def _make(name: str | None) -> None:
        from qwenpaw.app.routers import tool_calls as tc_module

        monkeypatch.setattr(tc_module, "is_auth_enabled", lambda: True)
        monkeypatch.setattr(
            tc_module,
            "get_request_username",
            lambda _request: name,
        )
        monkeypatch.setattr(tc_module, "load_config", lambda: SimpleNamespace())

    return _make


def test_tool_calls_refuse_an_anonymous_caller(client, coordinator, as_user):
    """With auth on, no identity means no access (fail closed)."""
    as_user(None)

    resp = client.get("/api/tool-calls/session-a")

    assert resp.status_code == 401


def test_get_call_refuses_another_accounts_agent(
    client,
    coordinator,
    as_user,
    monkeypatch,
):
    from fastapi import HTTPException

    from qwenpaw.app.routers import tool_calls as tc_module

    as_user("eve")
    coordinator.get.return_value = _running_entry(session_id="session-a")

    def _deny(_config, _username, _agent_id):
        raise HTTPException(status_code=404, detail="Agent not found")

    monkeypatch.setattr(tc_module, "require_owned_agent_id", _deny)

    resp = client.get("/api/tool-calls/session-a/tc-1")

    assert resp.status_code == 404


def test_cancel_refuses_another_accounts_agent(
    client,
    coordinator,
    as_user,
    monkeypatch,
):
    from fastapi import HTTPException

    from qwenpaw.app.routers import tool_calls as tc_module

    as_user("eve")
    coordinator.get.return_value = _running_entry(session_id="session-a")

    def _deny(_config, _username, _agent_id):
        raise HTTPException(status_code=404, detail="Agent not found")

    monkeypatch.setattr(tc_module, "require_owned_agent_id", _deny)

    resp = client.post("/api/tool-calls/session-a/tc-1/cancel")

    assert resp.status_code == 404
    coordinator.cancel.assert_not_called()


def test_list_calls_hides_other_accounts_entries(
    client,
    coordinator,
    as_user,
    monkeypatch,
):
    from qwenpaw.app.routers import tool_calls as tc_module

    as_user("eve")
    coordinator.list_entries.return_value = [_running_entry()]
    monkeypatch.setattr(tc_module, "is_agent_owned", lambda _c, _u, _a: False)

    resp = client.get("/api/tool-calls/session-a")

    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_list_calls_keeps_owned_entries(
    client,
    coordinator,
    as_user,
    monkeypatch,
):
    from qwenpaw.app.routers import tool_calls as tc_module

    as_user("bob")
    coordinator.list_entries.return_value = [_running_entry()]
    monkeypatch.setattr(tc_module, "is_agent_owned", lambda _c, _u, _a: True)

    resp = client.get("/api/tool-calls/session-a")

    assert resp.status_code == 200
    assert resp.json()["total"] == 1
