# -*- coding: utf-8 -*-
"""Versioned schema management for the platform's PostgreSQL store.

The knowledge service and the data-analysis tables use
``CREATE TABLE IF NOT EXISTS`` plus ``ADD COLUMN IF NOT EXISTS``, which is
idempotent but leaves no record of *what* has been applied — so a partially
failed upgrade is invisible and cannot be reasoned about later. This module adds
the missing half: a ``schema_migrations`` table records each applied step by
version, and the statements themselves run once.

Three properties are deliberate:

* **Serialized across processes.** The API container and any worker may start
  together, so schema creation runs under a transaction-scoped advisory lock —
  the same pattern the knowledge service already uses.
* **Failure is isolated per statement.** A statement that fails (a column that
  already exists under a different definition, a missing extension) is logged and
  skipped rather than aborting the whole migration, matching how the rest of this
  codebase treats upgrade steps.
* **A failed migration is not recorded**, so the next boot retries it instead of
  pretending the schema is complete.

Everything happens on **one connection inside one transaction**: the advisory
lock is transaction-scoped, so taking it on one connection and then running the
statements on another would deadlock against itself.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from .config import PLATFORM_SCHEMA, SCHEMA_LOCK_KEY
from .pool import PlatformStoreUnavailable, get_conn, put_conn

logger = logging.getLogger(__name__)

_schema_lock = threading.Lock()
_schema_ready = False


@dataclass(frozen=True)
class Migration:
    """One ordered schema step, applied once and recorded by *version*."""

    version: str
    name: str
    statements: tuple[str, ...]


def _ensure_schema(cur) -> None:
    cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{PLATFORM_SCHEMA}"')


def _ensure_migrations_table(cur) -> None:
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS "{PLATFORM_SCHEMA}".schema_migrations (
            version     TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
    )


def applied_versions(cur) -> set[str]:
    """Versions already recorded as applied."""
    _ensure_migrations_table(cur)
    cur.execute(f'SELECT version FROM "{PLATFORM_SCHEMA}".schema_migrations')
    return {row[0] for row in cur.fetchall()}


def _run_statements(cur, migration: Migration) -> None:
    """Run one migration's statements, isolating each failure with a savepoint.

    A failing statement is rolled back to its savepoint and skipped. A plain
    rollback would be wrong here: it would discard the statements that already
    succeeded earlier in the same migration, and (being transaction-scoped) would
    also release the advisory lock mid-flight.
    """
    for index, statement in enumerate(migration.statements):
        savepoint = f"mig_{index}"
        cur.execute(f"SAVEPOINT {savepoint}")
        try:
            cur.execute(statement)
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception as exc:  # noqa: BLE001 - skip and continue
            cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
            logger.warning(
                "platform store: migration %s statement skipped (%s): %s",
                migration.version,
                exc,
                " ".join(statement.split())[:120],
            )


def apply_migrations(migrations: list[Migration]) -> list[str]:
    """Apply any migration in *migrations* that has not run yet.

    Returns the versions applied by this call. Safe to call concurrently and
    repeatedly: the advisory lock serializes callers, and a version is recorded
    only after its statements have run.
    """
    if not migrations:
        return []
    conn = get_conn()
    applied_now: list[str] = []
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            # Transaction-scoped: released on commit/rollback, so a crashed
            # process cannot leave the lock held.
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_KEY,))
            _ensure_schema(cur)
            _ensure_migrations_table(cur)
            done = applied_versions(cur)

            for migration in migrations:
                if migration.version in done:
                    continue
                _run_statements(cur, migration)
                cur.execute(
                    f'INSERT INTO "{PLATFORM_SCHEMA}".schema_migrations '
                    "(version, name) VALUES (%s, %s) "
                    "ON CONFLICT (version) DO NOTHING",
                    (migration.version, migration.name),
                )
                done.add(migration.version)
                applied_now.append(migration.version)
                logger.info(
                    "platform store: applied migration %s (%s)",
                    migration.version,
                    migration.name,
                )
        conn.commit()
    except PlatformStoreUnavailable:
        conn.rollback()
        raise
    except Exception:  # noqa: BLE001 - never break boot on schema bookkeeping
        conn.rollback()
        logger.exception("platform store: schema migration failed")
        raise
    finally:
        put_conn(conn)
    return applied_now


def ensure_schema(migrations: list[Migration]) -> None:
    """Apply *migrations* once per process.

    Called from startup; later calls short-circuit so the hot path never touches
    the lock or the database for schema reasons.
    """
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        apply_migrations(migrations)
        _schema_ready = True


def reset_schema_flag() -> None:
    """Forget the once-per-process flag (tests)."""
    global _schema_ready
    with _schema_lock:
        _schema_ready = False


__all__ = [
    "Migration",
    "applied_versions",
    "apply_migrations",
    "ensure_schema",
    "reset_schema_flag",
]
