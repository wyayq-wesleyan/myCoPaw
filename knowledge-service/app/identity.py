"""Signed caller identity for the knowledge service.

The service is deployed on the host network and is reachable from the LAN, so
``X-User-Id`` alone is not an identity: anyone who can reach the port could
claim to be any account. The main application signs the account it resolved
from its own session, and this module verifies the signature before any handler
touches the store.

The secret is deliberately *not* the API key when a separate one is configured.
The API key is sent on every request, so reusing it as an HMAC key only proves
"the caller has the key", not "the main application minted this identity". For
deployments that have not configured a separate secret yet, the API key is used
as a fallback so behaviour is no worse than today; a warning points at the
recommended setting.
"""

from __future__ import annotations

import hashlib
import hmac
import time

# A request may be in flight for a while (uploads, migrations), but the
# signature itself is minted immediately before the call. Five minutes absorbs
# clock drift between containers without leaving a replay window that matters
# for a read-mostly internal API.
DEFAULT_MAX_SKEW_SECONDS = 300


def _message(username: str, timestamp: int) -> bytes:
    return f"{username}\n{timestamp}".encode("utf-8")


def sign_identity(secret: str, username: str, timestamp: int | None = None) -> tuple[int, str]:
    """Return ``(timestamp, signature)`` for *username*.

    Shared implementation: the main app imports the same function so the two
    sides cannot drift on message layout.
    """
    ts = int(time.time()) if timestamp is None else int(timestamp)
    digest = hmac.new(
        secret.encode("utf-8"),
        _message(username, ts),
        hashlib.sha256,
    ).hexdigest()
    return ts, digest


def verify_identity(
    secret: str,
    username: str | None,
    timestamp: str | int | None,
    signature: str | None,
    *,
    now: int | None = None,
    max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS,
) -> tuple[bool, str]:
    """Verify one signed identity.

    Returns ``(ok, reason)``. ``reason`` is safe to log server-side and is
    intentionally not returned to the caller (it would help an attacker tune).
    """
    if not secret:
        # Legacy mode: no secret configured. The caller decided to run this way.
        return True, "identity secret not configured; accepting unsigned identity"
    if not username:
        return False, "missing user"
    if timestamp in (None, ""):
        return False, "missing timestamp"
    if not signature:
        return False, "missing signature"
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False, "malformed timestamp"
    current = int(time.time()) if now is None else int(now)
    if abs(current - ts) > max(0, int(max_skew_seconds)):
        return False, "timestamp outside the allowed skew"
    expected = hmac.new(
        secret.encode("utf-8"),
        _message(str(username), ts),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, str(signature)):
        return False, "signature mismatch"
    return True, "ok"
