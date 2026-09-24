# -*- coding: utf-8 -*-
"""Token usage manager — thin orchestrator.
"""

import logging
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from ..constant import WORKING_DIR, TOKEN_USAGE_FILE
from .buffer import TokenUsageBuffer, _UsageEvent

logger = logging.getLogger(__name__)


class TokenUsageStats(BaseModel):
    """Prompt/completion tokens and call count."""

    prompt_tokens: int = Field(0, ge=0)
    completion_tokens: int = Field(0, ge=0)
    call_count: int = Field(0, ge=0)


class TokenUsageRecord(TokenUsageStats):
    """Single row from token usage query (per date + provider + model)."""

    date: str = Field(..., description="Date (YYYY-MM-DD)")
    provider_id: str = Field("", description="Provider ID")
    model: str = Field(..., description="Model name")


class TokenUsageByModel(TokenUsageStats):
    """Per-model aggregate in summary (provider + model + counts)."""

    provider_id: str = Field("", description="Provider ID")
    model: str = Field(..., description="Model name")


class TokenUsageByDateModel(TokenUsageStats):
    """Per-date per-model aggregate in summary."""

    provider_id: str = Field("", description="Provider ID")
    model: str = Field(..., description="Model name")


class TokenUsageSummary(BaseModel):
    """Aggregated token usage summary returned by get_summary()."""

    total_prompt_tokens: int = Field(0, ge=0)
    total_completion_tokens: int = Field(0, ge=0)
    total_calls: int = Field(0, ge=0)
    by_model: dict[str, TokenUsageByModel] = Field(
        default_factory=dict,
        description="Per model (provider:model key) aggregation",
    )
    by_date: dict[str, TokenUsageStats] = Field(
        default_factory=dict,
        description="Per date (YYYY-MM-DD) - all models combined",
    )


class AccountUsage(TokenUsageStats):
    """One account's usage inside the administrator view."""

    user_id: str = Field(..., description="Account username")
    by_date: dict[str, TokenUsageStats] = Field(
        default_factory=dict,
        description="Per date (YYYY-MM-DD) breakdown for this account",
    )
    by_model: dict[str, TokenUsageStats] = Field(
        default_factory=dict,
        description="Per model breakdown for this account",
    )


class AccountUsageSummary(BaseModel):
    """Per-account token usage summary for administrators."""

    total_prompt_tokens: int = Field(0, ge=0)
    total_completion_tokens: int = Field(0, ge=0)
    total_calls: int = Field(0, ge=0)
    by_user: dict[str, AccountUsage] = Field(
        default_factory=dict,
        description="Per account aggregation",
    )
    by_date: dict[str, TokenUsageStats] = Field(
        default_factory=dict,
        description="Per date totals across every account",
    )
    unattributed: TokenUsageStats = Field(
        default_factory=TokenUsageStats,
        description=(
            "Usage recorded before the account dimension existed, or that "
            "could not be attributed to an account"
        ),
    )


def _sum_user_entries(by_user: dict) -> dict:
    """Total the per-account breakdown of one provider:model entry."""
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "call_count": 0}
    for user_entry in by_user.values():
        if not isinstance(user_entry, dict):
            continue
        totals["prompt_tokens"] += user_entry.get("prompt_tokens", 0) or 0
        totals["completion_tokens"] += user_entry.get("completion_tokens", 0) or 0
        totals["call_count"] += user_entry.get("call_count", 0) or 0
    return totals


class TokenUsageManager:
    """Orchestrator for token usage recording and querying."""

    _instance: "TokenUsageManager | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        # The path is retained as the buffer's identity only: usage is stored
        # in the platform PostgreSQL database, not in this file.
        path: Path = (WORKING_DIR / TOKEN_USAGE_FILE).expanduser()
        self._buffer = TokenUsageBuffer(path)
        self._flush_interval = 10  # default

    def start(self, flush_interval: int = 10) -> None:
        """Start background flush task.

        Must be called from an async context (e.g. app lifespan startup).
        ``flush_interval`` is the number of seconds between flushes.
        """
        self._flush_interval = flush_interval
        # Recreate buffer with desired flush_interval if different from default
        if flush_interval != 10:
            path: Path = (WORKING_DIR / TOKEN_USAGE_FILE).expanduser()
            self._buffer = TokenUsageBuffer(
                path,
                flush_interval=flush_interval,
            )
        self._buffer.start()

    async def stop(self) -> None:
        """Stop the flush task and perform a final flush before exit."""
        await self._buffer.stop()

    def enqueue(self, event: _UsageEvent) -> None:
        """Synchronous fire-and-forget — enqueue a pre-built usage event.

        Called directly from ``TokenRecordingModelWrapper._record_usage()``
        on the hot path. No ``await`` required.
        """
        self._buffer.enqueue(event)

    async def record(
        self,
        provider_id: str,
        model_name: str,
        prompt_tokens: int,
        completion_tokens: int,
        at_date: Optional[date] = None,
    ) -> None:
        """Record token usage for a given provider, model and date.

        Convenience async wrapper around ``enqueue()`` for callers that
        prefer the original async interface (e.g. tests, skill tools).

        Args:
            provider_id: ID of the provider (e.g. "dashscope", "openai").
            model_name: Name of the model (e.g. "qwen3-max", "gpt-4").
            prompt_tokens: Number of input/prompt tokens.
            completion_tokens: Number of output/completion tokens.
            at_date: Date to record under. Defaults to today (local).
        """
        from datetime import datetime, timezone

        if at_date is None:
            at_date = date.today()
        self._buffer.enqueue(
            _UsageEvent(
                provider_id=provider_id,
                model_name=model_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                date_str=at_date.isoformat(),
                now_iso=datetime.now(tz=timezone.utc).isoformat(
                    timespec="seconds",
                ),
            ),
        )

    async def _query(
        self,
        merged: dict,
        start_date: date,
        end_date: date,
        model_name: Optional[str],
        provider_id: Optional[str],
        user_filter: Optional[str] = None,
    ) -> list[TokenUsageRecord]:
        """Return per-day records from the merged data dict.

        ``user_filter`` narrows the result to one account. It reads that
        account's own counters rather than the entry totals, because the
        totals also carry usage that could not be attributed to anyone; a
        person's figures must not include other people's (or legacy) usage.
        """
        results: list[TokenUsageRecord] = []

        current = start_date
        while current <= end_date:
            date_str = current.isoformat()
            by_key = merged.get(date_str, {})
            for _key, entry in by_key.items():
                if not isinstance(entry, dict):
                    continue
                rec_provider = entry.get("provider_id", "")
                rec_model = entry.get("model_name") or _key
                if model_name is not None and rec_model != model_name:
                    continue
                if provider_id is not None and rec_provider != provider_id:
                    continue

                if user_filter is not None:
                    user_entry = (entry.get("by_user") or {}).get(user_filter)
                    if not isinstance(user_entry, dict):
                        # No attributed usage for this account on this model.
                        continue
                    prompt = user_entry.get("prompt_tokens", 0) or 0
                    completion = user_entry.get("completion_tokens", 0) or 0
                    calls = user_entry.get("call_count", 0) or 0
                else:
                    prompt = entry.get("prompt_tokens", 0) or 0
                    completion = entry.get("completion_tokens", 0) or 0
                    calls = entry.get("call_count", 0) or 0

                results.append(
                    TokenUsageRecord(
                        date=date_str,
                        provider_id=rec_provider,
                        model=rec_model,
                        prompt_tokens=prompt,
                        completion_tokens=completion,
                        call_count=calls,
                    ),
                )
            current += timedelta(days=1)

        return results

    async def get_summary(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        model_name: Optional[str] = None,
        provider_id: Optional[str] = None,
        user_filter: Optional[str] = None,
    ) -> TokenUsageSummary:
        """Get aggregated token usage summary.

        Args:
            start_date: Start of date range (inclusive). Default: 30 days ago.
            end_date: End of date range (inclusive). Default: today.
            model_name: Optional model name filter.
            provider_id: Optional provider ID filter.
            user_filter: Optional account to scope the result to. ``None``
                returns the global view; callers must have resolved
                authorization before passing an unscoped value.

        Returns:
            TokenUsageSummary with totals, by_model, by_provider, by_date.
        """
        if end_date is None:
            end_date = date.today()
        if start_date is None:
            start_date = end_date - timedelta(days=30)

        merged = await self._buffer.get_merged_data()

        records = await self._query(
            merged,
            start_date,
            end_date,
            model_name,
            provider_id,
            user_filter,
        )

        total_prompt = 0
        total_completion = 0
        total_calls = 0
        by_model_raw: dict[str, dict] = {}
        by_date_raw: dict[str, dict] = {}

        for r in records:
            pt = r.prompt_tokens
            ct = r.completion_tokens
            calls = r.call_count
            total_prompt += pt
            total_completion += ct
            total_calls += calls

            # Aggregate by model
            model_key = (
                f"{r.provider_id}:{r.model}" if r.provider_id else r.model
            )
            bm = by_model_raw.setdefault(
                model_key,
                {
                    "provider_id": r.provider_id,
                    "model": r.model,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "call_count": 0,
                },
            )
            bm["prompt_tokens"] += pt
            bm["completion_tokens"] += ct
            bm["call_count"] += calls

            # Aggregate by date
            bd = by_date_raw.setdefault(
                r.date,
                {"prompt_tokens": 0, "completion_tokens": 0, "call_count": 0},
            )
            bd["prompt_tokens"] += pt
            bd["completion_tokens"] += ct
            bd["call_count"] += calls

        return TokenUsageSummary(
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
            total_calls=total_calls,
            by_model={
                k: TokenUsageByModel.model_validate(v)
                for k, v in sorted(by_model_raw.items())
            },
            by_date={
                k: TokenUsageStats.model_validate(v)
                for k, v in sorted(by_date_raw.items())
            },
        )

    async def get_details(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        model_name: Optional[str] = None,
        provider_id: Optional[str] = None,
        user_filter: Optional[str] = None,
    ) -> list[TokenUsageRecord]:
        """Get raw token usage records for frontend aggregation.

        Args:
            start_date: Start of date range (inclusive). Default: 30 days ago.
            end_date: End of date range (inclusive). Default: today.
            model_name: Optional model name filter.
            provider_id: Optional provider ID filter.
            user_filter: Optional account to scope the result to. ``None``
                returns the global view; callers must have resolved
                authorization before passing an unscoped value.

        Returns:
            List of TokenUsageRecord with per-date per-model data.
        """
        if end_date is None:
            end_date = date.today()
        if start_date is None:
            start_date = end_date - timedelta(days=30)

        merged = await self._buffer.get_merged_data()

        records = await self._query(
            merged,
            start_date,
            end_date,
            model_name,
            provider_id,
            user_filter,
        )

        return records

    @classmethod
    def get_instance(cls) -> "TokenUsageManager":
        """Return the process-wide singleton ``TokenUsageManager``."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    async def get_account_usage(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        users: Optional[list[str]] = None,
    ) -> "AccountUsageSummary":
        """Aggregate token usage per account for the administrator view.

        Reads the ``by_user`` breakdown stored beside each global counter.
        Events recorded before the account dimension existed carry no
        ``by_user``; they are reported separately as ``unattributed`` instead
        of being silently dropped, so the totals still reconcile.

        Args:
            start_date: Start of date range (inclusive). Default: 30 days ago.
            end_date: End of date range (inclusive). Default: today.
            users: Optional account allowlist; when given, only these accounts
                are returned in ``by_user`` (unattributed counts stay visible
                so the figures remain auditable).
        """
        if end_date is None:
            end_date = date.today()
        if start_date is None:
            start_date = end_date - timedelta(days=30)

        merged = await self._buffer.get_merged_data()
        wanted = set(users) if users else None

        by_user_raw: dict[str, dict] = {}
        by_date_raw: dict[str, dict] = {}
        unattributed: dict = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "call_count": 0,
        }
        total_prompt = 0
        total_completion = 0
        total_calls = 0

        current = start_date
        while current <= end_date:
            date_str = current.isoformat()
            for _key, entry in (merged.get(date_str) or {}).items():
                if not isinstance(entry, dict):
                    continue
                day_prompt = entry.get("prompt_tokens", 0) or 0
                day_completion = entry.get("completion_tokens", 0) or 0
                day_calls = entry.get("call_count", 0) or 0
                total_prompt += day_prompt
                total_completion += day_completion
                total_calls += day_calls
                # by_date can legitimately exceed the sum of person-days when
                # some calls predate account attribution.
                bucket = by_date_raw.setdefault(
                    date_str,
                    {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "call_count": 0,
                    },
                )
                bucket["prompt_tokens"] += day_prompt
                bucket["completion_tokens"] += day_completion
                bucket["call_count"] += day_calls

                by_user = entry.get("by_user") or {}
                # An entry can mix attributed and pre-upgrade usage (a model
                # used both before and after the account dimension existed),
                # so derive the unattributed part per entry instead of
                # treating "has by_user" as "fully attributed".
                attributed = _sum_user_entries(by_user)
                unattributed["prompt_tokens"] += max(
                    0, day_prompt - attributed["prompt_tokens"],
                )
                unattributed["completion_tokens"] += max(
                    0, day_completion - attributed["completion_tokens"],
                )
                unattributed["call_count"] += max(
                    0, day_calls - attributed["call_count"],
                )

                for user_id, user_entry in by_user.items():
                    if wanted is not None and user_id not in wanted:
                        continue
                    record = by_user_raw.setdefault(
                        user_id,
                        {
                            "user_id": user_id,
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "call_count": 0,
                            "by_date": {},
                            "by_model": {},
                        },
                    )
                    record["prompt_tokens"] += user_entry.get("prompt_tokens", 0) or 0
                    record["completion_tokens"] += (
                        user_entry.get("completion_tokens", 0) or 0
                    )
                    record["call_count"] += user_entry.get("call_count", 0) or 0

                    user_day = record["by_date"].setdefault(
                        date_str,
                        {
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "call_count": 0,
                        },
                    )
                    user_day["prompt_tokens"] += user_entry.get("prompt_tokens", 0) or 0
                    user_day["completion_tokens"] += (
                        user_entry.get("completion_tokens", 0) or 0
                    )
                    user_day["call_count"] += user_entry.get("call_count", 0) or 0

                    model_key = entry.get("model_name") or _key
                    model_entry = record["by_model"].setdefault(
                        model_key,
                        {
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "call_count": 0,
                        },
                    )
                    model_entry["prompt_tokens"] += user_entry.get("prompt_tokens", 0) or 0
                    model_entry["completion_tokens"] += (
                        user_entry.get("completion_tokens", 0) or 0
                    )
                    model_entry["call_count"] += user_entry.get("call_count", 0) or 0
            current += timedelta(days=1)

        return AccountUsageSummary(
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
            total_calls=total_calls,
            by_user={
                user_id: AccountUsage.model_validate(record)
                for user_id, record in by_user_raw.items()
            },
            by_date={
                date_str: TokenUsageStats.model_validate(stats)
                for date_str, stats in by_date_raw.items()
            },
            unattributed=TokenUsageStats.model_validate(unattributed),
        )


def get_token_usage_manager() -> TokenUsageManager:
    """Return the process-wide singleton ``TokenUsageManager``."""
    return TokenUsageManager.get_instance()
