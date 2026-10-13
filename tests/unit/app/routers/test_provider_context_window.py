# -*- coding: utf-8 -*-
"""Tests for active-model context-window metadata."""

from types import SimpleNamespace

from qwenpaw.app.routers.providers import _active_models_info
from qwenpaw.config.config import ModelSlotConfig


def test_active_models_info_uses_runtime_context_resolution():
    provider = SimpleNamespace(get_context_size=lambda _model_id: 1_000_000)
    manager = SimpleNamespace(get_provider=lambda _provider_id: provider)
    slot = ModelSlotConfig(provider_id="dashscope", model="qwen3.7-max")

    info = _active_models_info(manager, slot)

    assert info.active_llm == slot
    assert info.effective_max_input_length == 1_000_000


# --------------------------------------------------------------------------- #
# The chat page reads the model through this helper, so the "no default model"
# behaviour has to be observable here — otherwise a user with an entitlement
# would see an empty selector even though the runtime would pick a model.
# --------------------------------------------------------------------------- #


def _manager(provider_id="local", model="m1", context=8192):
    provider = SimpleNamespace(get_context_size=lambda _model_id: context)
    return SimpleNamespace(
        get_provider=lambda _provider_id: provider,
        _default=(provider_id, model),
    )


def test_unauthorized_configured_slot_is_replaced_by_the_account_default(monkeypatch):
    """A revoked entitlement must not leave the user with no usable model.

    The admin-chosen slot is dropped for this account, and the account's own
    first authorized model takes its place instead of the selector going empty.
    """
    monkeypatch.setattr(
        "qwenpaw.app.routers.providers.is_model_allowed",
        lambda _u, _p, _m: False,
    )
    monkeypatch.setattr(
        "qwenpaw.app.routers.providers.first_allowed_model",
        lambda _u: ("local", "m1"),
    )
    manager = _manager()

    info = _active_models_info(
        manager,
        ModelSlotConfig(provider_id="admin-only", model="secret"),
        "alice",
    )

    assert (info.active_llm.provider_id, info.active_llm.model) == ("local", "m1")


def test_authorized_configured_slot_is_kept(monkeypatch):
    """An existing install keeps running the model it was already using."""
    monkeypatch.setattr(
        "qwenpaw.app.routers.providers.is_model_allowed",
        lambda _u, _p, _m: True,
    )
    monkeypatch.setattr(
        "qwenpaw.app.routers.providers.first_allowed_model",
        lambda _u: ("local", "fallback"),
    )
    manager = _manager()

    slot = ModelSlotConfig(provider_id="local", model="chosen")
    info = _active_models_info(manager, slot, "alice")

    assert info.active_llm == slot


def test_no_entitlement_yields_no_model(monkeypatch):
    """Nothing authorized means an explicit empty result, not a guess."""
    monkeypatch.setattr(
        "qwenpaw.app.routers.providers.first_allowed_model",
        lambda _u: None,
    )
    manager = _manager()

    info = _active_models_info(manager, None, "alice")

    assert info.active_llm is None
    assert info.effective_max_input_length is None


def test_fallback_can_be_disabled(monkeypatch):
    """``scope=agent`` must report the agent's slot verbatim, with no default."""
    monkeypatch.setattr(
        "qwenpaw.app.routers.providers.first_allowed_model",
        lambda _u: ("local", "m1"),
    )
    manager = _manager()

    info = _active_models_info(manager, None, "alice", allow_implicit_fallback=False)

    assert info.active_llm is None
