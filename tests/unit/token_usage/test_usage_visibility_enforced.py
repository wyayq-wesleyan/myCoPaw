"""A normal account must never receive another account's usage.

Three separate code paths can report usage, and all three used to return the
combined global total to whoever asked:
  - ``GET /token-usage``
  - ``GET /token-usage/details``
  - the ``get_token_usage`` agent tool (answers "how many tokens have I used?")
Each is pinned here, because fixing only one of them would still leak.
"""

from __future__ import annotations

import importlib
from datetime import date
from types import SimpleNamespace

import pytest

from qwenpaw.app.routers import token_usage as router_module
from qwenpaw.token_usage.buffer import _UsageEvent, _apply_event
from qwenpaw.token_usage.manager import TokenUsageManager

# ``qwenpaw.agents.tools`` re-exports the function under the module's name, so
# fetch the module itself rather than the attribute.
tool_module = importlib.import_module("qwenpaw.agents.tools.get_token_usage")

DAY = date.today().isoformat()


class _FakeBuffer:
    def __init__(self, cache: dict) -> None:
        self._cache = cache

    async def get_merged_data(self) -> dict:
        return self._cache


def _manager() -> TokenUsageManager:
    """A manager over a shared store holding two accounts' usage."""
    cache: dict = {}
    _apply_event(cache, _UsageEvent("prov", "model-a", 100, 10, DAY, "t", "alice"))
    _apply_event(cache, _UsageEvent("prov", "model-a", 900, 90, DAY, "t", "bob"))
    manager = TokenUsageManager.__new__(TokenUsageManager)
    manager._buffer = _FakeBuffer(cache)
    return manager


def _request():
    return SimpleNamespace(headers={})


@pytest.fixture
def two_accounts(monkeypatch):
    """Wire the shared manager and script the caller's role."""
    monkeypatch.setattr(router_module, "get_token_usage_manager", _manager)
    monkeypatch.setattr(tool_module, "get_token_usage_manager", _manager)

    def _as(username: str, is_admin: bool):
        monkeypatch.setattr(
            router_module,
            "get_request_username",
            lambda _r: username,
        )
        # visible_account imports this lazily from app.user_space.
        monkeypatch.setattr(
            "qwenpaw.app.user_space.is_admin_user",
            lambda _u: is_admin,
        )
        monkeypatch.setattr(tool_module, "_resolve_caller", lambda: username)

    return _as


async def test_summary_endpoint_scopes_a_regular_account(two_accounts):
    two_accounts("alice", is_admin=False)

    summary = await router_module.get_token_usage(
        request=_request(),
        start_date=DAY,
        end_date=DAY,
        model=None,
        provider=None,
    )

    # Only alice's 110 tokens, not the shared 1100.
    assert summary.total_prompt_tokens == 100
    assert summary.total_completion_tokens == 10
    assert summary.total_calls == 1


async def test_summary_endpoint_gives_administrators_everything(two_accounts):
    two_accounts("admin", is_admin=True)

    summary = await router_module.get_token_usage(
        request=_request(),
        start_date=DAY,
        end_date=DAY,
        model=None,
        provider=None,
    )

    assert summary.total_prompt_tokens == 1000
    assert summary.total_calls == 2


async def test_details_endpoint_scopes_a_regular_account(two_accounts):
    two_accounts("alice", is_admin=False)

    records = await router_module.get_token_usage_details(
        request=_request(),
        start_date=DAY,
        end_date=DAY,
        model=None,
        provider=None,
    )

    assert sum(r.prompt_tokens for r in records) == 100


async def test_details_endpoint_gives_administrators_everything(two_accounts):
    two_accounts("admin", is_admin=True)

    records = await router_module.get_token_usage_details(
        request=_request(),
        start_date=DAY,
        end_date=DAY,
        model=None,
        provider=None,
    )

    assert sum(r.prompt_tokens for r in records) == 1000


async def test_agent_tool_scopes_a_regular_account(two_accounts):
    """The chat answer to "how many tokens have I used?" must be own-only."""
    two_accounts("alice", is_admin=False)

    chunk = await tool_module.get_token_usage(days=1)
    text = chunk.content[0].text

    assert "110" in text
    assert "1,100" not in text
    assert "1,000" not in text


async def test_agent_tool_gives_administrators_everything(two_accounts):
    two_accounts("admin", is_admin=True)

    chunk = await tool_module.get_token_usage(days=1)

    assert "1,100" in chunk.content[0].text


async def test_manager_filters_out_other_accounts(two_accounts):
    """The filter reads the account's own counters, not the entry totals."""
    summary = await _manager().get_summary(
        start_date=date.fromisoformat(DAY),
        end_date=date.fromisoformat(DAY),
        user_filter="bob",
    )

    assert summary.total_prompt_tokens == 900
    assert summary.total_calls == 1


async def test_manager_filter_excludes_unattributed_usage(two_accounts):
    """Legacy usage with no owner must not inflate a personal figure."""
    cache: dict = {}
    _apply_event(cache, _UsageEvent("prov", "model-a", 500, 50, DAY, "t"))
    _apply_event(cache, _UsageEvent("prov", "model-a", 100, 10, DAY, "t", "alice"))
    manager = TokenUsageManager.__new__(TokenUsageManager)
    manager._buffer = _FakeBuffer(cache)

    summary = await manager.get_summary(
        start_date=date.fromisoformat(DAY),
        end_date=date.fromisoformat(DAY),
        user_filter="alice",
    )

    assert summary.total_prompt_tokens == 100
    assert summary.total_calls == 1
