"""Skill transfer endpoints must stay usable without opening cross-account paths.

Regression: an earlier round locked the whole ``/skills/pool/*`` surface to
administrators because nothing checked ownership. That was too blunt — uploading
one's own skill into the shared pool and pulling a pooled skill into one's own
workspace are ordinary Skills-page actions, so normal accounts hit 403 (and the
route then broke outright). The correct boundary is the *target agent*: an
account may transfer skills for the agents it owns.

Two real defects are pinned here:
1. ``/skills/pool/upload|download`` refused normal accounts (and raised 500 from
   a wrong relative import once touched);
2. the same endpoints accepted an arbitrary ``workspace_id``.
"""

from __future__ import annotations

import inspect

import pytest


def _route_dependencies(router, path: str, method: str) -> list:
    for route in router.routes:
        if getattr(route, "path", None) == path and method in (
            getattr(route, "methods", set()) or set()
        ):
            return list(getattr(route, "dependencies", []) or [])
    return []


def test_workspace_transfer_endpoints_are_not_admin_only():
    """These are normal user actions, so they must not require an admin."""
    from qwenpaw.app.routers import skills as skills_module

    for method, path in (
        ("POST", "/skills/pool/upload"),
        ("POST", "/skills/pool/download"),
    ):
        deps = _route_dependencies(skills_module.router, path, method)
        assert not deps, (
            f"{method} {path} must not carry a router-level admin guard; "
            "transferring skills for one's own agents is a normal action"
        )


def test_pool_management_endpoints_remain_admin_only():
    """Global pool content is still an administrator's to change."""
    from qwenpaw.app.routers._admin_guard import require_admin_request
    from qwenpaw.app.routers import skills as skills_module

    for method, path in (
        ("POST", "/skills/pool/create"),
        ("PUT", "/skills/pool/save"),
        ("POST", "/skills/pool/import"),
        ("POST", "/skills/pool/import-builtin"),
        ("DELETE", "/skills/pool/{skill_name}"),
        ("POST", "/skills/pool/batch-delete"),
    ):
        deps = _route_dependencies(skills_module.router, path, method)
        assert any(
            getattr(dep, "dependency", None) is require_admin_request
            for dep in deps
        ), f"{method} {path} must stay administrator-only"


def test_transfer_checks_the_target_agent():
    """The body supplies workspace_id, so ownership has to be verified."""
    from qwenpaw.app.routers import skills as skills_module

    upload_src = inspect.getsource(
        skills_module.upload_workspace_skill_to_pool,
    )
    assert "_require_agent_accessible(request, body.workspace_id)" in upload_src

    download_src = inspect.getsource(
        skills_module.download_pool_skill_to_workspaces,
    )
    assert "_require_agent_accessible(request, target.workspace_id)" in download_src


def test_all_workspaces_stays_administrator_only():
    """Targeting every workspace crosses accounts by definition."""
    from qwenpaw.app.routers import skills as skills_module

    source = inspect.getsource(
        skills_module.download_pool_skill_to_workspaces,
    )
    assert "body.all_workspaces" in source
    assert "is_admin_user" in source


def test_require_agent_accessible_allows_an_owned_agent(monkeypatch):
    from qwenpaw.app.routers import skills as skills_module

    monkeypatch.setattr(
        "qwenpaw.app.user_space.get_request_username",
        lambda _request: "bob",
    )
    monkeypatch.setattr(
        "qwenpaw.app.user_space.is_admin_user",
        lambda _username: False,
    )
    monkeypatch.setattr(
        "qwenpaw.app.user_space.is_agent_owned",
        lambda _config, _username, _agent: True,
    )
    monkeypatch.setattr(
        skills_module,
        "load_config",
        lambda: object(),
        raising=False,
    )
    # Must not raise.
    skills_module._require_agent_accessible(object(), "bob-bot")


def test_require_agent_accessible_rejects_another_accounts_agent(monkeypatch):
    from fastapi import HTTPException

    from qwenpaw.app.routers import skills as skills_module

    monkeypatch.setattr(
        "qwenpaw.app.user_space.get_request_username",
        lambda _request: "bob",
    )
    monkeypatch.setattr(
        "qwenpaw.app.user_space.is_admin_user",
        lambda _username: False,
    )
    monkeypatch.setattr(
        "qwenpaw.app.user_space.is_agent_owned",
        lambda _config, _username, _agent: False,
    )
    monkeypatch.setattr(
        skills_module,
        "load_config",
        lambda: object(),
        raising=False,
    )

    with pytest.raises(HTTPException) as excinfo:
        skills_module._require_agent_accessible(object(), "admin-bot")

    assert excinfo.value.status_code == 403


def test_require_agent_accessible_allows_an_admin(monkeypatch):
    """Administrators keep deployment-wide reach."""
    from qwenpaw.app.routers import skills as skills_module

    monkeypatch.setattr(
        "qwenpaw.app.user_space.get_request_username",
        lambda _request: "admin",
    )
    monkeypatch.setattr(
        "qwenpaw.app.user_space.is_admin_user",
        lambda _username: True,
    )

    skills_module._require_agent_accessible(object(), "anyones-agent")
