"""Per-account token usage for the administrator view.

The account dimension is additive: the global counters every existing reader
uses must stay identical, and usage recorded before accounts were tracked must
remain visible as `unattributed` rather than silently disappearing.
"""

from __future__ import annotations

from datetime import date

import pytest

from qwenpaw.token_usage.buffer import _UsageEvent, _apply_event
from qwenpaw.token_usage.manager import TokenUsageManager

DAY = "2026-09-18"


def _event(prompt: int, completion: int, user: str = "", day: str = DAY):
    return _UsageEvent("prov", "model-a", prompt, completion, day, "t", user)


class _FakeBuffer:
    """Stand-in for TokenUsageBuffer backed by a prepared cache dict."""

    def __init__(self, cache: dict) -> None:
        self._cache = cache

    async def get_merged_data(self) -> dict:
        return self._cache


def _manager(cache: dict) -> TokenUsageManager:
    manager = TokenUsageManager.__new__(TokenUsageManager)
    manager._buffer = _FakeBuffer(cache)
    return manager


def _cache(*events) -> dict:
    cache: dict = {}
    for event in events:
        _apply_event(cache, event)
    return cache


async def test_usage_is_grouped_per_account():
    cache = _cache(
        _event(100, 10, "alice"),
        _event(50, 5, "bob"),
        _event(30, 3, "alice"),
    )

    summary = await _manager(cache).get_account_usage(
        start_date=date(2026, 9, 18),
        end_date=date(2026, 9, 18),
    )

    assert set(summary.by_user) == {"alice", "bob"}
    assert summary.by_user["alice"].prompt_tokens == 130
    assert summary.by_user["alice"].completion_tokens == 13
    assert summary.by_user["alice"].call_count == 2
    assert summary.by_user["bob"].prompt_tokens == 50
    # Totals cover every account.
    assert summary.total_prompt_tokens == 180
    assert summary.total_calls == 3


async def test_global_counters_are_unchanged_by_the_account_dimension():
    """A shared model must not be split or double-counted at the top level."""
    cache = _cache(_event(100, 10, "alice"), _event(50, 5, "bob"))

    entry = cache[DAY]["prov:model-a"]
    assert entry["prompt_tokens"] == 150
    assert entry["completion_tokens"] == 15
    assert entry["call_count"] == 2
    # One row per provider:model, not one per account.
    assert len(cache[DAY]) == 1

    summary = await _manager(cache).get_account_usage(
        start_date=date(2026, 9, 18),
        end_date=date(2026, 9, 18),
    )
    assert summary.total_prompt_tokens == 150
    assert sum(u.prompt_tokens for u in summary.by_user.values()) == 150


async def test_pre_upgrade_usage_is_reported_as_unattributed():
    """Events from before the account dimension stay visible, not dropped."""
    cache = _cache(_event(100, 10), _event(50, 5, "alice"))

    summary = await _manager(cache).get_account_usage(
        start_date=date(2026, 9, 18),
        end_date=date(2026, 9, 18),
    )

    assert summary.unattributed.prompt_tokens == 100
    assert summary.unattributed.call_count == 1
    assert summary.by_user["alice"].prompt_tokens == 50
    # Totals include the unattributed part so the figures still reconcile.
    assert summary.total_prompt_tokens == 150
    assert (
        sum(account.prompt_tokens for account in summary.by_user.values())
        + summary.unattributed.prompt_tokens
        == summary.total_prompt_tokens
    )


async def test_account_allowlist_filters_the_breakdown():
    cache = _cache(_event(100, 10, "alice"), _event(50, 5, "bob"))

    summary = await _manager(cache).get_account_usage(
        start_date=date(2026, 9, 18),
        end_date=date(2026, 9, 18),
        users=["alice"],
    )

    assert set(summary.by_user) == {"alice"}
    # Totals intentionally still describe the whole range.
    assert summary.total_prompt_tokens == 150


async def test_per_account_daily_and_model_breakdown():
    cache = _cache(
        _event(10, 1, "alice", day="2026-09-17"),
        _event(20, 2, "alice", day="2026-09-18"),
    )

    summary = await _manager(cache).get_account_usage(
        start_date=date(2026, 9, 17),
        end_date=date(2026, 9, 18),
    )

    alice = summary.by_user["alice"]
    assert alice.by_date["2026-09-17"].prompt_tokens == 10
    assert alice.by_date["2026-09-18"].prompt_tokens == 20
    assert alice.by_model["model-a"].prompt_tokens == 30
    # Range totals are trimmed to the requested window.
    assert set(summary.by_date) == {"2026-09-17", "2026-09-18"}


async def test_dates_outside_the_range_are_excluded():
    cache = _cache(_event(10, 1, "alice", day="2026-09-01"))

    summary = await _manager(cache).get_account_usage(
        start_date=date(2026, 9, 18),
        end_date=date(2026, 9, 18),
    )

    assert summary.by_user == {}
    assert summary.total_prompt_tokens == 0


async def test_malformed_entries_do_not_break_aggregation():
    cache = {DAY: {"prov:model-a": "not-a-dict", "bad": None}}

    summary = await _manager(cache).get_account_usage(
        start_date=date(2026, 9, 18),
        end_date=date(2026, 9, 18),
    )

    assert summary.total_prompt_tokens == 0


@pytest.mark.parametrize("account", ["alice", "bob"])
async def test_each_account_sees_only_its_own_row(account):
    cache = _cache(_event(100, 10, "alice"), _event(50, 5, "bob"))

    summary = await _manager(cache).get_account_usage(
        start_date=date(2026, 9, 18),
        end_date=date(2026, 9, 18),
        users=[account],
    )

    assert list(summary.by_user) == [account]
