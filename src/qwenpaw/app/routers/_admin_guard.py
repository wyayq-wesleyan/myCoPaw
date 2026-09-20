"""Shared FastAPI dependencies for administrator-only routes.

Applied at router level (``dependencies=[Depends(...)]``) rather than per
endpoint, so a route added later cannot silently ship without the check — the
failure mode this module exists to prevent.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from ..auth import require_admin_access
from ..user_space import get_request_username


def require_admin_request(request: Request) -> None:
    """Reject the request unless it comes from an administrator.

    Fails closed: a request whose identity cannot be resolved is refused rather
    than treated as privileged. ``require_admin_access`` already rejects an
    unknown account, so an absent identity lands on the same 403.
    """
    username = get_request_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    require_admin_access(username)


__all__ = ["require_admin_request"]
