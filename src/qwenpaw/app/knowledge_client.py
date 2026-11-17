# -*- coding: utf-8 -*-
"""Single place that talks to the standalone knowledge service.

Before this module the knowledge URL, API key and caller identity were assembled
independently in the router, the agent tools, the ReAct auto-injection and the
chat-attachment parser. Four copies of a security header drift; the service
cannot tell which caller is trustworthy, and a fix in one path silently misses
the others.

Two rules live here:

* the API key authenticates the *application*;
* the signed ``X-User-Id``/``X-User-Ts``/``X-User-Sig`` triple authenticates the
  *account* the main app resolved from its own session. The service verifies it
  before touching any document, so a process that can reach port 18100 cannot
  impersonate an account by editing a header.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any

import httpx

# Keep in sync with knowledge-service/app/identity.py.
DEFAULT_MAX_SKEW_SECONDS = 300
_DOCUMENT_LOOKUP_TIMEOUT = 10.0


def service_url() -> str:
    return os.environ.get("QWENPAW_KNOWLEDGE_URL", "").strip().rstrip("/")


def api_key() -> str:
    return os.environ.get("QWENPAW_KNOWLEDGE_API_KEY", "").strip()


def identity_secret() -> str:
    """Secret shared with the knowledge service for identity signatures.

    A dedicated secret is preferred; the API key is the fallback so existing
    deployments get signed identities without a new required setting.
    """
    return (
        os.environ.get("QWENPAW_KNOWLEDGE_IDENTITY_SECRET", "").strip()
        or api_key()
    )


def sign_identity(
    username: str,
    timestamp: int | None = None,
    *,
    secret: str | None = None,
) -> tuple[int, str]:
    """Return ``(timestamp, signature)`` for *username*.

    The message layout is fixed (``username\\ntimestamp``) and duplicated in the
    service by design: the two deploy separately, so they cannot share a Python
    import, but they must agree byte-for-byte.
    """
    ts = int(time.time()) if timestamp is None else int(timestamp)
    signing_key = identity_secret() if secret is None else secret
    digest = hmac.new(
        signing_key.encode("utf-8"),
        f"{username}\n{ts}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return ts, digest


def service_headers(username: str) -> dict[str, str]:
    """Headers for one account-scoped knowledge call."""
    headers = {"X-User-Id": username}
    secret = identity_secret()
    if secret:
        timestamp, signature = sign_identity(username, secret=secret)
        headers["X-User-Ts"] = str(timestamp)
        headers["X-User-Sig"] = signature
    key = api_key()
    if key:
        headers["X-Knowledge-Key"] = key
    return headers


def service_key_headers() -> dict[str, str]:
    """Headers for identity-agnostic endpoints (e.g. ``/v1/documents/parse``)."""
    key = api_key()
    return {"X-Knowledge-Key": key} if key else {}


async def document_exists(
    username: str,
    document_id: str,
    *,
    timeout: float = _DOCUMENT_LOOKUP_TIMEOUT,
) -> bool | None:
    """Whether *document_id* belongs to *username*.

    ``True``/``False`` are authoritative 200/404 answers. ``None`` means the
    service is not configured, unreachable, or answered unexpectedly — callers
    must fail closed (deny the operation) rather than treat it as "missing" or
    "allowed".
    """
    url = service_url()
    if not url or not document_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{url}/v1/documents/{document_id}",
                headers=service_headers(username),
            )
    except httpx.HTTPError:
        return None
    if response.status_code == 200:
        return True
    if response.status_code == 404:
        return False
    return None


async def account_document_ids(
    client: httpx.AsyncClient,
    base_url: str,
    username: str,
    *,
    page_size: int = 100,
) -> list[str]:
    """Every knowledge document id owned by *username*.

    Used to turn the path model into the explicit allow-list the service filters
    on. Paginated because a large account legitimately has thousands of rows and
    the service caps ``page_size`` at 100.
    """
    documents: list[str] = []
    page = 1
    while True:
        response = await client.get(
            f"{base_url}/v1/documents",
            headers=service_headers(username),
            params={"page": page, "page_size": page_size},
        )
        response.raise_for_status()
        payload: Any = response.json()
        items = payload.get("items") if isinstance(payload, dict) else payload
        items = items or []
        documents.extend(
            str(item.get("id")) for item in items if item.get("id")
        )
        total = payload.get("total") if isinstance(payload, dict) else None
        if len(items) < page_size or (
            total is not None and len(documents) >= int(total)
        ):
            break
        page += 1
        if page > 1000:  # defensive bound, same as the router's
            break
    return documents
