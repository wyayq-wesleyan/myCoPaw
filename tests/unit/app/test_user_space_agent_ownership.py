from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from qwenpaw.app import user_space


def _config():
    return SimpleNamespace(
        agents=SimpleNamespace(
            active_agent="user_alice_default",
            profiles={
                "user_admin_default": SimpleNamespace(owner_username="admin"),
                "user_alice_default": SimpleNamespace(owner_username="alice"),
                "user_bob_default": SimpleNamespace(owner_username="bob"),
            },
        ),
    )


def test_administrator_lists_only_its_own_agents(monkeypatch):
    config = _config()
    monkeypatch.setattr(user_space, "_migrate_user_workspace_paths", lambda _username: None)
    monkeypatch.setattr(user_space, "load_config", lambda: config)
    monkeypatch.setattr(user_space, "load_user_state", lambda _username: {})

    assert user_space.user_agent_ids(config, "admin") == ["user_admin_default"]


def test_administrator_cannot_access_another_accounts_agent(monkeypatch):
    config = _config()
    monkeypatch.setattr(user_space, "ensure_user_default_agent", lambda _username: None)
    monkeypatch.setattr(user_space, "load_config", lambda: config)

    assert user_space.is_agent_owned(config, "admin", "user_admin_default")
    assert not user_space.is_agent_owned(config, "admin", "user_alice_default")

    with pytest.raises(HTTPException) as exc_info:
        user_space.require_owned_agent_id(config, "admin", "user_alice_default")

    assert exc_info.value.status_code == 404


def test_regular_user_still_lists_only_its_own_agents(monkeypatch):
    config = _config()
    monkeypatch.setattr(user_space, "_migrate_user_workspace_paths", lambda _username: None)
    monkeypatch.setattr(user_space, "load_config", lambda: config)
    monkeypatch.setattr(user_space, "load_user_state", lambda _username: {})

    assert user_space.user_agent_ids(config, "alice") == ["user_alice_default"]
