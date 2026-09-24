# -*- coding: utf-8 -*-
"""Persistence for token usage, in the platform's PostgreSQL store.

The in-memory accumulator (:mod:`buffer`) owns the totals and periodically
snapshots the whole document; this module moves those bytes in and out of
``platform.token_usage``. The document shape is unchanged — callers still pass
and receive the same nested dict — so the aggregation and query logic above this
layer did not have to change.

**Why this is no longer a JSON file.** ``token_usage.json`` was one global
document for every account, rewritten in full on each flush. Two consequences
followed. Concurrent writers could read-modify-write the same file and lose each
other's increments. And the per-account breakdown was a nested dict inside the
document, so any question about one account meant loading every account's
numbers. Normalizing to one row per ``(date, provider, model, account)`` makes
each increment an atomic upsert and each question a query.

The legacy file is read once by the upgrade import (see
:mod:`..platform_store.legacy_import`); nothing here reads or writes it.
"""

from __future__ import annotations

import logging

import psycopg2

from ..platform_store import (
    PlatformStoreUnavailable,
    ensure_schema,
    get_conn,
    put_conn,
)
from ..platform_store.migrations import TOKEN_USAGE_MIGRATIONS

logger = logging.getLogger(__name__)

# Row key sentinel for usage with no account. A real value rather than NULL so
# the primary key's uniqueness covers it.
_UNATTRIBUTED = ""

_schema_ready = False


def _ensure_ready() -> None:
    """Apply the token-usage schema once per process (tolerating a down DB)."""
    global _schema_ready
    if _schema_ready:
        return
    if ensure_schema(TOKEN_USAGE_MIGRATIONS):
        _schema_ready = True


async def load_data(_path=None) -> dict:
    """Rebuild the usage document from PostgreSQL.

    ``_path`` is accepted and ignored so callers that still pass the legacy file
    path keep working; the storage location is the platform database now.
    Returns the same nested shape the JSON file held::

        { "2026-04-23": { "provider:model": {
              "provider_id": ..., "model_name": ...,
              "prompt_tokens": ..., "completion_tokens": ...,
              "call_count": ...,
              "by_user": {"alice": {...}},
        } } }

    Entry-level counters are the sum over every account (including the
    unattributed bucket), which is what the pre-existing readers expect. Still a
    coroutine because its only caller awaits it while seeding the buffer cache;
    the query itself runs on the calling thread.
    """
    return _load_data_sync()


def _load_data_sync() -> dict:
    """Blocking body of :func:`load_data`."""
    _ensure_ready()
    try:
        conn = get_conn()
    except PlatformStoreUnavailable:
        # Reading usage must not be what prevents the platform from booting.
        logger.warning(
            "token_usage: platform database unavailable — starting empty",
        )
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT used_on, provider_id, model_name, user_id, "
                "prompt_tokens, completion_tokens, call_count "
                "FROM platform.token_usage"
            )
            rows = cur.fetchall()
    except psycopg2.Error:
        logger.exception("token_usage: could not read usage")
        raise
    finally:
        put_conn(conn)

    document: dict = {}
    for (
        used_on,
        provider_id,
        model_name,
        user_id,
        prompt_tokens,
        completion_tokens,
        call_count,
    ) in rows:
        day_bucket = document.setdefault(used_on, {})
        entry = day_bucket.setdefault(
            f"{provider_id}:{model_name}",
            {
                "provider_id": provider_id,
                "model_name": model_name,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "call_count": 0,
            },
        )
        entry["prompt_tokens"] += int(prompt_tokens or 0)
        entry["completion_tokens"] += int(completion_tokens or 0)
        entry["call_count"] += int(call_count or 0)
        if user_id:
            entry.setdefault("by_user", {})[user_id] = {
                "prompt_tokens": int(prompt_tokens or 0),
                "completion_tokens": int(completion_tokens or 0),
                "call_count": int(call_count or 0),
            }
    return document


def iter_rows(document: dict):
    """Flatten the nested document into ``(key, counters)`` rows.

    Yields one row per account plus one unattributed row holding the entry total
    minus what the accounts account for, so reconstructing the document returns
    the same numbers that went in.
    """
    for used_on, day_bucket in (document or {}).items():
        if not isinstance(day_bucket, dict):
            continue
        for entry in day_bucket.values():
            if not isinstance(entry, dict):
                continue
            provider_id = str(entry.get("provider_id") or "")
            model_name = str(entry.get("model_name") or "")
            if not model_name:
                # The composite key is provider:model; without a model there is
                # nothing to attribute usage to, so skip rather than invent one.
                logger.warning(
                    "token_usage: skipping entry with no model_name on %s",
                    used_on,
                )
                continue
            by_user = entry.get("by_user")
            if not isinstance(by_user, dict):
                by_user = {}

            attributable = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "call_count": 0,
            }
            for account, counts in by_user.items():
                if not isinstance(counts, dict):
                    continue
                row = {
                    "prompt_tokens": int(counts.get("prompt_tokens") or 0),
                    "completion_tokens": int(
                        counts.get("completion_tokens") or 0
                    ),
                    "call_count": int(counts.get("call_count") or 0),
                }
                for field in attributable:
                    attributable[field] += row[field]
                yield (used_on, provider_id, model_name, str(account)), row

            # Whatever the entry total holds beyond the accounts is
            # unattributed usage (pre-upgrade events, or a caller that never set
            # an account).
            yield (
                (used_on, provider_id, model_name, _UNATTRIBUTED),
                {
                    "prompt_tokens": max(
                        0,
                        int(entry.get("prompt_tokens") or 0)
                        - attributable["prompt_tokens"],
                    ),
                    "completion_tokens": max(
                        0,
                        int(entry.get("completion_tokens") or 0)
                        - attributable["completion_tokens"],
                    ),
                    "call_count": max(
                        0,
                        int(entry.get("call_count") or 0)
                        - attributable["call_count"],
                    ),
                },
            )


def save_data_sync(_path=None, data: dict | None = None) -> bool:
    """Persist a usage document, returning ``True`` on success.

    Deliberately synchronous so the buffer's flush task can call it through
    ``asyncio.to_thread`` without blocking the event loop.

    The whole document is upserted, which is safe because it is a cumulative
    snapshot: a row's values only grow, and ``GREATEST`` keeps a concurrent flush
    from lowering a count it read before another writer incremented it. Rows are
    never deleted here.
    """
    # Tolerate the historical ``save_data_sync(path, data)`` positional form as
    # well as the keyword form the buffer uses.
    if data is None and isinstance(_path, dict):
        data = _path
    if not isinstance(data, dict):
        data = {}

    _ensure_ready()
    try:
        conn = get_conn()
    except PlatformStoreUnavailable:
        logger.warning(
            "token_usage: platform database unavailable — snapshot not saved",
        )
        return False
    try:
        with conn.cursor() as cur:
            for key, counts in iter_rows(data):
                cur.execute(
                    """
                    INSERT INTO platform.token_usage
                        (used_on, provider_id, model_name, user_id,
                         prompt_tokens, completion_tokens, call_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (used_on, provider_id, model_name, user_id)
                    DO UPDATE SET
                        prompt_tokens =
                            GREATEST(platform.token_usage.prompt_tokens,
                                     EXCLUDED.prompt_tokens),
                        completion_tokens =
                            GREATEST(platform.token_usage.completion_tokens,
                                     EXCLUDED.completion_tokens),
                        call_count =
                            GREATEST(platform.token_usage.call_count,
                                     EXCLUDED.call_count)
                    """,
                    (
                        *key,
                        counts["prompt_tokens"],
                        counts["completion_tokens"],
                        counts["call_count"],
                    ),
                )
        conn.commit()
        return True
    except Exception:  # noqa: BLE001 - never break the flush loop
        conn.rollback()
        logger.exception("token_usage: failed to save snapshot")
        return False
    finally:
        put_conn(conn)


def upsert_rows(rows) -> int:
    """Additive insert of ``((date, provider, model, account), stats)`` rows.

    Used by the legacy import, where the numbers being written are a one-time
    copy rather than a cumulative snapshot: it must *add* to whatever is already
    present instead of taking maxima, so the imported totals combine with usage
    recorded since the upgrade. Accepts the same ``(key, counters)`` pairs
    :func:`iter_rows` produces.
    """
    written = 0
    _ensure_ready()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for key, stats in rows:
                used_on, provider_id, model_name, user_id = key
                cur.execute(
                    """
                    INSERT INTO platform.token_usage
                        (used_on, provider_id, model_name, user_id,
                         prompt_tokens, completion_tokens, call_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (used_on, provider_id, model_name, user_id)
                    DO UPDATE SET
                        prompt_tokens =
                            platform.token_usage.prompt_tokens
                            + EXCLUDED.prompt_tokens,
                        completion_tokens =
                            platform.token_usage.completion_tokens
                            + EXCLUDED.completion_tokens,
                        call_count =
                            platform.token_usage.call_count
                            + EXCLUDED.call_count
                    """,
                    (
                        used_on,
                        provider_id,
                        model_name,
                        user_id,
                        int(stats.get("prompt_tokens") or 0),
                        int(stats.get("completion_tokens") or 0),
                        int(stats.get("call_count") or 0),
                    ),
                )
                written += 1
        conn.commit()
        return written
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def reset_schema_flag() -> None:
    """Forget the once-per-process schema flag (tests)."""
    global _schema_ready
    _schema_ready = False


__all__ = [
    "iter_rows",
    "load_data",
    "reset_schema_flag",
    "save_data_sync",
    "upsert_rows",
]
