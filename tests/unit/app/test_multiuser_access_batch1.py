"""Batch-1 access-control fixes found by the 30-user architecture review.

Each of these was confirmed on a real two-account deployment (admin + bob,
authentication enabled) before the fix:

* ``GET /api/models/default-models`` returned provider credentials in plaintext
  to any authenticated account;
* the whole ``/api/skills/pool/*`` surface (plus ``GET /api/skills/workspaces``)
  had no identity check, so any account could read or rewrite the shared skill
  pool and, via ``workspace_id``, another account's workspace skills;
* ``POST /api/fork/agent`` relied only on a localhost check, which is not an
  authorization boundary behind a same-host proxy;
* the console inbox/push handlers read ``request.state.user`` directly and a
  ``None`` identity silently disabled filtering;
* three ``PUT /api/workspace/*`` endpoints wrote deployment-wide settings
  without an admin check;
* ``GET /api/console/chat/task/{task_id}`` returned another account's task.

These assert the guard is wired at the routing layer, which is where the
regression would appear.
"""

from __future__ import annotations

import inspect

import pytest


def _route_dependencies(router, path: str, method: str) -> list:
    """Return the router-level dependencies attached to one route."""
    for route in router.routes:
        if getattr(route, "path", None) == path and method in (
            getattr(route, "methods", set()) or set()
        ):
            return list(getattr(route, "dependencies", []) or [])
    return []


def _has_admin_guard(router, path: str, method: str) -> bool:
    from qwenpaw.app.routers._admin_guard import require_admin_request

    return any(
        getattr(dep, "dependency", None) is require_admin_request
        for dep in _route_dependencies(router, path, method)
    )


# --------------------------------------------------------- provider credentials


def test_default_models_requires_an_administrator():
    """The response carries plaintext provider api keys."""
    from qwenpaw.app.routers import providers as providers_module

    source = inspect.getsource(providers_module.get_default_models)
    assert "_require_admin_model_access(request)" in source


# ------------------------------------------------------------- shared skill pool


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/skills/pool/create"),
        ("PUT", "/skills/pool/save"),
        ("POST", "/skills/pool/upload-zip"),
        ("POST", "/skills/pool/import"),
        # NOTE: /skills/pool/upload and /skills/pool/download are deliberately
        # NOT here. They move a skill between the pool and the caller's *own*
        # agent, which is an ordinary Skills-page action; an earlier round
        # guarded them too and broke normal use for non-admin accounts. Their
        # boundary is ownership of the target agent, asserted in
        # test_skill_transfer_access.py.
        ("POST", "/skills/pool/import-builtin"),
        ("POST", "/skills/pool/{skill_name}/update-builtin"),
        ("DELETE", "/skills/pool/{skill_name}"),
        ("PUT", "/skills/pool/{skill_name}/config"),
        ("DELETE", "/skills/pool/{skill_name}/config"),
        ("PUT", "/skills/pool/{skill_name}/tags"),
        ("PUT", "/skills/pool/{skill_name}/auto-update"),
        ("POST", "/skills/pool/batch-delete"),
        ("GET", "/skills/workspaces"),
    ],
)
def test_shared_skill_pool_is_admin_only(method, path):
    """Pool operations touch every account's skills, so they are admin-only."""
    from qwenpaw.app.routers import skills as skills_module

    assert _has_admin_guard(skills_module.router, path, method), (
        f"{method} {path} is missing the admin guard"
    )


# ------------------------------------------------------------------ fork agent


def test_fork_agent_enforces_agent_ownership():
    """Localhost is not an authorization boundary."""
    from qwenpaw.app.routers import fork as fork_module

    source = inspect.getsource(fork_module.fork_agent)
    assert "_enforce_owned_agent(request, req.agent_id)" in source


def test_fork_ownership_helper_fails_closed_without_identity(monkeypatch):
    from fastapi import HTTPException

    from qwenpaw.app.routers import fork as fork_module

    monkeypatch.setattr(fork_module, "is_auth_enabled", lambda: True, raising=False)
    # The helper imports these lazily, so patch at the source modules.
    import qwenpaw.app.auth as auth_module
    import qwenpaw.app.user_space as user_space_module

    monkeypatch.setattr(auth_module, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(
        user_space_module,
        "get_request_username",
        lambda _request: None,
    )

    with pytest.raises(HTTPException) as excinfo:
        fork_module._enforce_owned_agent(object(), "some-agent")

    assert excinfo.value.status_code == 401


# -------------------------------------------------------- console task ownership


def test_background_task_records_its_owner():
    from qwenpaw.app.routers import console as console_module

    assert "owner" in console_module._BackgroundTask.__annotations__


def test_task_status_refuses_another_accounts_task(monkeypatch):
    import asyncio

    from fastapi import HTTPException

    from qwenpaw.app.routers import console as console_module

    task = console_module._BackgroundTask(owner="alice", status="running")
    console_module._bg_tasks["task-testowner"] = task
    monkeypatch.setattr(
        "qwenpaw.app.user_space.get_request_username",
        lambda _request: "bob",
    )
    monkeypatch.setattr(
        console_module,
        "get_request_username",
        lambda _request: "bob",
        raising=False,
    )
    try:
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(
                console_module.get_console_chat_task("task-testowner", object()),
            )
        assert excinfo.value.status_code == 404
    finally:
        console_module._bg_tasks.pop("task-testowner", None)


# ------------------------------------------------------------ global settings


@pytest.mark.parametrize(
    "handler",
    ["put_audio_mode", "put_transcription_provider_type", "put_transcription_provider"],
)
def test_deployment_wide_settings_require_an_administrator(handler):
    from qwenpaw.app.routers import workspace as workspace_module

    source = inspect.getsource(getattr(workspace_module, handler))
    assert "require_admin_access" in source, (
        f"{handler} writes a deployment-wide setting without an admin check"
    )
