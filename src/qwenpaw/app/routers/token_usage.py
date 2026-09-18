# -*- coding: utf-8 -*-
"""Token usage API for console and skill tool."""

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, Request

from ...token_usage import (
    get_token_usage_manager,
    AccountUsageSummary,
    TokenUsageSummary,
    TokenUsageRecord,
)
from ...token_usage.visibility import visible_account
from ..auth import require_admin_access
from ..user_space import get_request_username

router = APIRouter(prefix="/token-usage", tags=["token-usage"])


def _parse_date(s: str | None) -> date | None:
    """Parse YYYY-MM-DD string to date."""
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _require_admin(request: Request) -> None:
    """Allow only administrators to read cross-account statistics."""
    username = get_request_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    require_admin_access(username)


@router.get(
    "",
    summary="Get token usage summary",
    description=(
        "Return aggregated token usage by date, model, and provider. "
        "Administrators see every account; other accounts see only their own."
    ),
)
async def get_token_usage(
    request: Request,
    start_date: str
    | None = Query(
        None,
        description="Start date YYYY-MM-DD (inclusive). Default: 30 days ago",
    ),
    end_date: str
    | None = Query(
        None,
        description="End date YYYY-MM-DD (inclusive). Default: today",
    ),
    model: str
    | None = Query(
        None,
        description="Filter by model name",
    ),
    provider: str
    | None = Query(
        None,
        description="Filter by provider ID",
    ),
) -> TokenUsageSummary:
    """Return aggregated token usage summary for the given date range."""
    end_d = _parse_date(end_date) or date.today()
    start_d = _parse_date(start_date) or (end_d - timedelta(days=30))
    if start_d > end_d:
        start_d, end_d = end_d, start_d

    return await get_token_usage_manager().get_summary(
        start_date=start_d,
        end_date=end_d,
        model_name=model,
        provider_id=provider,
        user_filter=visible_account(get_request_username(request)),
    )


@router.get(
    "/accounts",
    response_model=AccountUsageSummary,
    summary="Get per-account token usage (administrators only)",
    description=(
        "Return token usage grouped by account, plus per-date totals and an "
        "unattributed bucket for usage recorded before accounts were tracked."
    ),
)
async def get_account_usage(
    request: Request,
    start_date: str
    | None = Query(
        None,
        description="Start date YYYY-MM-DD (inclusive). Default: 30 days ago",
    ),
    end_date: str
    | None = Query(
        None,
        description="End date YYYY-MM-DD (inclusive). Default: today",
    ),
    accounts: str
    | None = Query(
        None,
        description=(
            "Comma-separated account allowlist. Omit to return every account "
            "with usage in the range."
        ),
    ),
) -> AccountUsageSummary:
    """Return per-account usage; administrators only."""
    _require_admin(request)

    end_d = _parse_date(end_date) or date.today()
    start_d = _parse_date(start_date) or (end_d - timedelta(days=30))
    if start_d > end_d:
        start_d, end_d = end_d, start_d

    selected = [part.strip() for part in (accounts or "").split(",") if part.strip()]
    return await get_token_usage_manager().get_account_usage(
        start_date=start_d,
        end_date=end_d,
        users=selected or None,
    )


@router.get(
    "/details",
    summary="Get token usage details",
    description=(
        "Return raw token usage records for frontend aggregation. "
        "Administrators see every account; other accounts see only their own."
    ),
)
async def get_token_usage_details(
    request: Request,
    start_date: str
    | None = Query(
        None,
        description="Start date YYYY-MM-DD (inclusive). Default: 30 days ago",
    ),
    end_date: str
    | None = Query(
        None,
        description="End date YYYY-MM-DD (inclusive). Default: today",
    ),
    model: str
    | None = Query(
        None,
        description="Filter by model name",
    ),
    provider: str
    | None = Query(
        None,
        description="Filter by provider ID",
    ),
) -> list[TokenUsageRecord]:
    """Return raw token usage records for the given date range."""
    end_d = _parse_date(end_date) or date.today()
    start_d = _parse_date(start_date) or (end_d - timedelta(days=30))
    if start_d > end_d:
        start_d, end_d = end_d, start_d

    return await get_token_usage_manager().get_details(
        start_date=start_d,
        end_date=end_d,
        model_name=model,
        provider_id=provider,
        user_filter=visible_account(get_request_username(request)),
    )
