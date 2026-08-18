from qwenpaw.app import auth, user_space


def test_regular_user_requires_explicit_model_permission(monkeypatch):
    monkeypatch.setattr(user_space, "get_model_permissions", lambda _username: None)
    monkeypatch.setattr(
        auth,
        "get_user_info",
        lambda _username: {"role": "user"},
    )

    assert not user_space.is_model_allowed("alice", "local", "model-a")


def test_administrator_remains_unrestricted_without_permission_record(monkeypatch):
    monkeypatch.setattr(user_space, "get_model_permissions", lambda _username: None)
    monkeypatch.setattr(
        auth,
        "get_user_info",
        lambda _username: {"role": "admin"},
    )

    assert user_space.is_model_allowed("admin", "local", "model-a")


def test_regular_user_can_only_use_explicitly_granted_models(monkeypatch):
    monkeypatch.setattr(
        user_space,
        "get_model_permissions",
        lambda _username: [{"provider_id": "local", "model_id": "model-a"}],
    )

    assert user_space.is_model_allowed("alice", "local", "model-a")
    assert not user_space.is_model_allowed("alice", "local", "model-b")
