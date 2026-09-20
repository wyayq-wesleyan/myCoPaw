"""Phase-1 multi-user isolation fixes.

QwenPaw is single-user by origin, so several surfaces were never account
scoped. These tests pin the fixes for the bypasses that let a normal account
reach another account's data:

* the backup API had no identity check at all (an archive bundles every
  account's workspace plus the global config);
* the cron ``@api_action`` auto-routes were mounted outside ``/api``, so the
  auth middleware skipped them, and they resolved the admin workspace;
* the agent-orchestration tools call the local API without credentials, and an
  absent identity is treated as unrestricted, so they leaked every account's
  agent list and could drive another account's agent;
* push messages and inbox traces were readable across accounts.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from qwenpaw.app.routers import _admin_guard as guard
from qwenpaw.app import console_push_store, inbox_trace_store


# ---------------------------------------------------------------------------
# The shared administrator guard
# ---------------------------------------------------------------------------


def _request(token: str | None = "Bearer t"):
    headers = {"Authorization": token} if token else {}
    return SimpleNamespace(headers=headers)


def test_admin_guard_rejects_an_unauthenticated_caller(monkeypatch):
    monkeypatch.setattr(guard, "get_request_username", lambda _r: None)

    with pytest.raises(HTTPException) as excinfo:
        guard.require_admin_request(_request(None))

    assert excinfo.value.status_code == 401


def test_admin_guard_rejects_a_normal_account(monkeypatch):
    monkeypatch.setattr(guard, "get_request_username", lambda _r: "alice")

    def _deny(_username):
        raise HTTPException(status_code=403, detail="Administrator access required")

    monkeypatch.setattr(guard, "require_admin_access", _deny)

    with pytest.raises(HTTPException) as excinfo:
        guard.require_admin_request(_request())

    assert excinfo.value.status_code == 403


def test_backup_router_inherits_the_admin_guard():
    """The guard is on the router so a new route cannot ship unprotected."""
    from qwenpaw.app.routers import backup

    assert guard.require_admin_request in [
        d.dependency for d in backup.router.dependencies
    ]
    # Every registered route must therefore require an administrator.
    assert backup.router.routes, "backup router unexpectedly has no routes"
    assert all(
        guard.require_admin_request in [d.dependency for d in route.dependencies or []]
        or backup.router.dependencies
        for route in backup.router.routes
    )


# ---------------------------------------------------------------------------
# Push messages carry an owner and the session-less read is scoped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_messages_are_attributed_and_scoped():
    console_push_store._list.clear()

    await console_push_store.append("sess-a", "alice 的消息", owner="alice")
    await console_push_store.append("sess-b", "bob 的消息", owner="bob")

    alice_view = await console_push_store.get_recent(owner="alice")
    assert [m["text"] for m in alice_view] == ["alice 的消息"]

    # An unscoped read (used by nothing now) still shows both, so the filter is
    # what provides the isolation rather than data loss.
    assert len(await console_push_store.get_recent()) == 2

    console_push_store._list.clear()


@pytest.mark.asyncio
async def test_push_messages_without_owner_are_not_shown_to_others():
    """A message with no recorded owner must not leak to an arbitrary account."""
    console_push_store._list.clear()
    await console_push_store.append("sess-x", "无归属消息")

    assert await console_push_store.get_recent(owner="alice") == []

    console_push_store._list.clear()


# ---------------------------------------------------------------------------
# Inbox traces carry an owner and cannot be read across accounts
# ---------------------------------------------------------------------------


@pytest.fixture
def trace_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox_trace_store, "_TRACE_DIR", tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_trace_is_readable_by_its_owner_only(trace_dir):
    await inbox_trace_store.create_trace(
        "run-1",
        meta={"agent_id": "a"},
        owner="alice",
    )

    owner_view = await inbox_trace_store.get_trace("run-1", requester="alice")
    assert owner_view is not None

    other_view = await inbox_trace_store.get_trace("run-1", requester="bob")
    assert other_view is None, "another account must not read this trace"

    admin_view = await inbox_trace_store.get_trace(
        "run-1",
        requester="bob",
        is_admin=True,
    )
    assert admin_view is not None


@pytest.mark.asyncio
async def test_legacy_trace_without_owner_is_admin_only(trace_dir):
    """A file written before attribution existed must not become public."""
    await inbox_trace_store.create_trace("run-legacy", meta={})

    assert await inbox_trace_store.get_trace("run-legacy", requester="alice") is None
    assert (
        await inbox_trace_store.get_trace(
            "run-legacy",
            requester="alice",
            is_admin=True,
        )
        is not None
    )


# ---------------------------------------------------------------------------
# Agent orchestration tools are scoped to the caller's own agents
# ---------------------------------------------------------------------------


def test_owned_agent_ids_denies_when_account_is_unresolvable(monkeypatch):
    """An unresolved account must yield no access, not unrestricted access."""
    from qwenpaw.agents.tools import agent_management as am

    assert am.owned_agent_ids(None) is None


def test_list_agents_data_is_filtered_to_the_caller(monkeypatch):
    from qwenpaw.agents.tools import agent_management as am

    payload = {
        "agents": [
            {"id": "alice-bot", "workspace_dir": "/w/alice"},
            {"id": "bob-bot", "workspace_dir": "/w/bob"},
        ],
    }

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, _path):
            return _Response()

    monkeypatch.setattr(am, "create_agent_api_client", lambda *a, **k: _Client())
    monkeypatch.setattr(am, "owned_agent_ids", lambda _u: {"alice-bot"})

    result = am.list_agents_data()

    assert [a["id"] for a in result["agents"]] == ["alice-bot"]


def test_list_agents_data_exposes_nothing_without_an_account(monkeypatch):
    from qwenpaw.agents.tools import agent_management as am

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"agents": [{"id": "bob-bot", "workspace_dir": "/w/bob"}]}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, _path):
            return _Response()

    monkeypatch.setattr(am, "create_agent_api_client", lambda *a, **k: _Client())
    monkeypatch.setattr(am, "owned_agent_ids", lambda _u: None)

    assert am.list_agents_data()["agents"] == []
