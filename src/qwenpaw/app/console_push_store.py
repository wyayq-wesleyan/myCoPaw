# -*- coding: utf-8 -*-
"""In-memory store for console channel push messages (e.g. cron text).

Bounded: at most _MAX_MESSAGES kept; messages older than _MAX_AGE_SECONDS
are dropped when reading. Frontend dedupes by id and caps its seen set.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Dict, List

# Single list: each item has id, text, ts, session_id and optional metadata.
# Bounded by count and age.
_list: List[Dict[str, Any]] = []
_lock = asyncio.Lock()
_MAX_AGE_SECONDS = 60
_MAX_MESSAGES = 500


async def append(
    session_id: str,
    text: str,
    *,
    sticky: bool = False,
    owner: str | None = None,
) -> None:
    """Append a message (bounded: oldest dropped if over _MAX_MESSAGES).

    ``owner`` records the account the message belongs to. The console polls
    this store without a session id, and the fallback used to return every
    account's message bodies; the owner lets that read be narrowed.
    """
    if not session_id or not text:
        return
    async with _lock:
        _list.append(
            {
                "id": str(uuid.uuid4()),
                "text": text,
                "sticky": sticky,
                "ts": time.time(),
                "session_id": session_id,
                "owner": owner or "",
            },
        )
        if len(_list) > _MAX_MESSAGES:
            _list.sort(key=lambda m: m["ts"])
            del _list[: len(_list) - _MAX_MESSAGES]


async def take(
    session_id: str,
    owner: str | None = None,
) -> List[Dict[str, Any]]:
    """Return and remove all messages for the session.

    When *owner* is given, only that account's messages are returned. A session
    id is not a credential — it travels in URLs and logs — so without this a
    caller that supplied another account's session id would consume (and thus
    delete) that account's queued message text.
    """
    if not session_id:
        return []
    async with _lock:
        _prune_expired_locked(_MAX_AGE_SECONDS)
        out = []
        remaining = []
        for msg in _list:
            if msg.get("session_id") != session_id:
                remaining.append(msg)
                continue
            if owner is not None and msg.get("owner") != owner:
                # Someone else's message under this session id: keep it queued
                # for its real owner rather than handing it over.
                remaining.append(msg)
                continue
            out.append(msg)
        _list[:] = remaining
        return _strip_ts(out)


async def take_all() -> List[Dict[str, Any]]:
    """Return and remove all non-expired messages from the store."""
    async with _lock:
        _prune_expired_locked(_MAX_AGE_SECONDS)
        out = list(_list)
        _list.clear()
        return _strip_ts(out)


def _strip_ts(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "id": m["id"],
            "text": m["text"],
            "sticky": bool(m.get("sticky", False)),
        }
        for m in msgs
    ]


def _prune_expired_locked(max_age_seconds: int) -> None:
    """Drop expired messages in-place. Caller must hold _lock."""
    cutoff = time.time() - max_age_seconds
    _list[:] = [m for m in _list if m["ts"] >= cutoff]


async def get_recent(
    max_age_seconds: int = _MAX_AGE_SECONDS,
    owner: str | None = None,
) -> List[Dict[str, Any]]:
    """
    Return recent messages (not consumed). Drop older than max_age_seconds
    from store to bound memory.

    When *owner* is given, only that account's messages are returned — the
    session-less console poll must not surface another account's message text.
    """
    if max_age_seconds < 0:
        raise ValueError("max_age_seconds must be non-negative")

    async with _lock:
        _prune_expired_locked(max_age_seconds)
        messages = _list
        if owner is not None:
            messages = [m for m in messages if m.get("owner") == owner]
        return _strip_ts(list(messages))
