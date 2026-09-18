# -*- coding: utf-8 -*-
"""Who may see whose token usage.

Usage is published to each account as a performance measure, so the rule is
simple and deliberately centralized: administrators see every account, and
everyone else sees only their own. Both the HTTP endpoints and the agent tool
resolve the identity (request identity vs. request context) differently, but
they must not disagree on the rule — hence one function.
"""

from __future__ import annotations

from typing import Optional


def visible_account(username: Optional[str]) -> Optional[str]:
    """Return the account whose usage may be shown, or ``None`` for all.

    ``None`` means "no account filter" — the global view, which only
    administrators get. A normal account is hard-scoped to itself.

    An empty *username* means the caller could not be identified (typically
    authentication disabled); the historical unrestricted view is returned so
    single-user and local deployments keep working.
    """
    if not username:
        return None

    from ..app.user_space import is_admin_user

    try:
        if is_admin_user(username):
            return None
    except Exception:  # noqa: BLE001 - never widen on a lookup failure
        return str(username)
    return str(username)


__all__ = ["visible_account"]
