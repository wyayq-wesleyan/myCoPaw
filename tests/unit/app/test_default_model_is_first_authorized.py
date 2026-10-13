# -*- coding: utf-8 -*-
"""The account's first authorized model is the conversation default.

Administrators no longer pin a global default model, so an account that has not
chosen one runs on the first model it is authorized to use. These tests pin the
resolution order, because that is what makes the model the chat selector shows
preselected identical to the model the backend actually runs.
"""

from types import SimpleNamespace

import pytest

from qwenpaw.app import user_space
from qwenpaw.providers import ProviderManager


def _model(model_id: str, model_type: str = "chat", source: str | None = "manual"):
    """Build a ModelInfo stand-in with an explicit or inferred purpose."""
    return SimpleNamespace(
        id=model_id,
        name=model_id,
        model_type=model_type,
        model_type_source=source,
    )


def _provider(provider_id: str, models, extra_models=()):
    return SimpleNamespace(
        id=provider_id,
        models=list(models),
        extra_models=list(extra_models),
    )


def _install_manager(monkeypatch, builtin=(), custom=(), legacy_slot=None):
    """Install a ProviderManager whose provider order is deterministic.

    ``legacy_slot`` is what the old administrator-pinned global model would
    hold; it is only consulted when the agent has no recorded owner.
    """
    manager = SimpleNamespace(
        builtin_providers={p.id: p for p in builtin},
        custom_providers={p.id: p for p in custom},
        get_active_model=lambda: legacy_slot,
    )
    monkeypatch.setattr(
        "qwenpaw.providers.ProviderManager.get_instance",
        staticmethod(lambda: manager),
    )
    return manager


def _grant(monkeypatch, allowed):
    """Authorize exactly ``allowed`` (provider_id, model_id) pairs."""
    monkeypatch.setattr(
        user_space,
        "get_model_permissions",
        lambda _username: [
            {"provider_id": provider_id, "model_id": model_id}
            for provider_id, model_id in allowed
        ],
    )


def test_returns_first_authorized_chat_model(monkeypatch):
    _install_manager(
        monkeypatch,
        builtin=[
            _provider("alpha", [_model("a1"), _model("a2")]),
            _provider("beta", [_model("b1")]),
        ],
    )
    _grant(monkeypatch, [("beta", "b1")])

    # Only beta/b1 is authorized, so it wins even though alpha comes first.
    assert user_space.first_allowed_model("alice") == ("beta", "b1")


def test_skips_embedding_and_reranker_models(monkeypatch):
    _install_manager(
        monkeypatch,
        builtin=[
            _provider(
                "alpha",
                [_model("embed", model_type="embedding"), _model("chat")],
                extra_models=[_model("rank", model_type="reranker")],
            ),
        ],
    )
    _grant(monkeypatch, [("alpha", "embed"), ("alpha", "chat"), ("alpha", "rank")])

    # An embedding model must never become the conversation model.
    assert user_space.first_allowed_model("alice") == ("alpha", "chat")


def test_infers_purpose_when_type_metadata_is_absent(monkeypatch):
    """Pre-existing providers have no stored purpose; it must be inferred.

    Without inference an embedding endpoint would be selected as the chat
    default purely because it happened to be registered first.
    """
    _install_manager(
        monkeypatch,
        builtin=[
            _provider(
                "alpha",
                [
                    _model("bge-m3-embedding", model_type="chat", source=None),
                    _model("qwen3-32b", model_type="chat", source=None),
                ],
            ),
        ],
    )
    _grant(monkeypatch, [("alpha", "bge-m3-embedding"), ("alpha", "qwen3-32b")])

    assert user_space.first_allowed_model("alice") == ("alpha", "qwen3-32b")


def test_custom_providers_are_considered_after_builtin(monkeypatch):
    _install_manager(
        monkeypatch,
        builtin=[_provider("alpha", [_model("a1")])],
        custom=[_provider("custom", [_model("c1")])],
    )
    _grant(monkeypatch, [("alpha", "a1"), ("custom", "c1")])

    assert user_space.first_allowed_model("alice") == ("alpha", "a1")


def test_extra_models_are_considered_after_provider_models(monkeypatch):
    _install_manager(
        monkeypatch,
        builtin=[
            _provider("alpha", [_model("main")], extra_models=[_model("added")]),
        ],
    )
    _grant(monkeypatch, [("alpha", "main"), ("alpha", "added")])

    assert user_space.first_allowed_model("alice") == ("alpha", "main")


def test_returns_none_when_account_has_no_entitlement(monkeypatch):
    _install_manager(monkeypatch, builtin=[_provider("alpha", [_model("a1")])])
    _grant(monkeypatch, [])

    assert user_space.first_allowed_model("alice") is None


def test_administrator_falls_back_to_every_configured_model(monkeypatch):
    _install_manager(monkeypatch, builtin=[_provider("alpha", [_model("a1")])])
    monkeypatch.setattr(user_space, "get_model_permissions", lambda _u: None)
    monkeypatch.setattr(
        "qwenpaw.app.auth.get_user_info",
        lambda _u: {"role": "admin"},
    )

    assert user_space.first_allowed_model("admin") == ("alpha", "a1")


def test_unauthenticated_caller_has_no_default(monkeypatch):
    """Authentication-disabled deployments resolve through the legacy slot."""
    _install_manager(monkeypatch, builtin=[_provider("alpha", [_model("a1")])])

    assert user_space.first_allowed_model(None) is None


def test_agent_owner_is_read_from_the_profile(monkeypatch):
    monkeypatch.setattr(
        user_space,
        "load_config",
        lambda: SimpleNamespace(
            agents=SimpleNamespace(
                profiles={"agent-1": SimpleNamespace(owner_username="alice")},
            ),
        ),
    )

    assert user_space.agent_owner_username("agent-1") == "alice"


def test_unknown_agent_has_no_owner(monkeypatch):
    monkeypatch.setattr(
        user_space,
        "load_config",
        lambda: SimpleNamespace(agents=SimpleNamespace(profiles={})),
    )

    assert user_space.agent_owner_username("missing") is None
    assert user_space.agent_owner_username(None) is None


# --------------------------------------------------------------------------- #
# ProviderManager.resolve_default_chat_slot — the runtime entry point
# --------------------------------------------------------------------------- #


def test_resolve_uses_owner_first_authorized_model(monkeypatch):
    _install_manager(monkeypatch, builtin=[_provider("alpha", [_model("a1")])])
    monkeypatch.setattr(
        user_space, "agent_owner_username", lambda _agent_id: "alice",
    )
    _grant(monkeypatch, [("alpha", "a1")])

    slot = ProviderManager.resolve_default_chat_slot("agent-1")

    assert (slot.provider_id, slot.model) == ("alpha", "a1")


def test_resolve_refuses_when_owner_has_no_model(monkeypatch):
    """An account with no entitlement must not silently inherit a global slot."""
    _install_manager(
        monkeypatch,
        builtin=[_provider("alpha", [_model("a1")])],
        legacy_slot=SimpleNamespace(provider_id="alpha", model="a1"),
    )
    monkeypatch.setattr(
        user_space, "agent_owner_username", lambda _agent_id: "alice",
    )
    _grant(monkeypatch, [])

    assert ProviderManager.resolve_default_chat_slot("agent-1") is None


def test_resolve_falls_back_to_legacy_slot_without_accounts(monkeypatch):
    """No owner means an authentication-disabled install: keep old behavior."""
    _install_manager(
        monkeypatch,
        legacy_slot=SimpleNamespace(provider_id="legacy", model="l1"),
    )
    monkeypatch.setattr(
        user_space, "agent_owner_username", lambda _agent_id: None,
    )

    slot = ProviderManager.resolve_default_chat_slot("agent-1")

    assert (slot.provider_id, slot.model) == ("legacy", "l1")


def test_resolve_tolerates_a_failing_profile_lookup(monkeypatch):
    """A broken config read must not crash request handling."""
    _install_manager(monkeypatch)

    def _boom(_agent_id):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(user_space, "agent_owner_username", _boom)

    assert ProviderManager.resolve_default_chat_slot("agent-1") is None
