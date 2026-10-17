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
# Versions this process has confirmed are recorded in `platform.schema_migrations`,
# and versions whose statements could not all run. Both are keyed per version
# because different callers ask for different migration sets — see `ensure_schema`.
_ready_versions: set[str] = set()
_incomplete_versions: set[str] = set()


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


def _run_statements(cur, migration: Migration) -> list[str]:
    """Run one migration's statements, isolating each failure with a savepoint.

    A failing statement is rolled back to its savepoint and skipped. A plain
    rollback would be wrong here: it would discard the statements that already
    succeeded earlier in the same migration, and (being transaction-scoped) would
    also release the advisory lock mid-flight.

    Returns the statements that were skipped, so the caller can refuse to record
    the migration as applied — see ``apply_migrations`` for why that matters.
    """
    skipped: list[str] = []
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
            skipped.append(" ".join(statement.split())[:120])
    return skipped


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
                skipped = _run_statements(cur, migration)
                if skipped:
                    # A skipped statement means the schema is NOT what this
                    # migration promises — a missing table, a missing extension.
                    # Recording it as applied made that permanent and silent: the
                    # version was never retried, so the gap surfaced much later as
                    # an unrelated-looking failure (a chat import 500ing on
                    # ``relation "platform.history_seq" does not exist``, with
                    # nothing in the log connecting it to the migration). Leave the
                    # version unrecorded so the next boot retries, and say so
                    # loudly — the statements are all IF-NOT-EXISTS, so re-running
                    # them costs nothing and self-heals as soon as the environment
                    # is fixed.
                    _incomplete_versions.add(migration.version)
                    logger.error(
                        "platform store: migration %s (%s) had %d statement(s) "
                        "skipped and is NOT recorded as applied; it will be "
                        "retried on the next start. First skipped: %s",
                        migration.version,
                        migration.name,
                        len(skipped),
                        skipped[0],
                    )
                    continue
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


def ensure_schema(migrations: list[Migration]) -> bool:
    """Apply *migrations* once per process, tolerating an unreachable database.

    Called from constructors on the startup path, so it must NOT raise when the
    database is down: the platform has to boot and report the affected features
    as unavailable, rather than crash-looping while PostgreSQL comes up. A
    failure is logged and answered with ``False``, and the bookkeeping stays
    unset so the next call retries instead of treating an empty schema as ready.

    The once-per-process cache is keyed on **versions**, not on a single boolean.
    Four callers pass four different sets — conversation history, governance
    audit, token usage, path scoping — and a bare "schema is ready" flag let
    whichever happened to run first answer for all of them: on a deployment where
    audit or token usage initialised first, the history tables were then never
    created, for the life of the process, and a restart did not help because the
    same caller won the race again. The symptom was a chat import failing with
    ``relation "platform.history_seq" does not exist`` while the database was
    perfectly reachable and some other platform tables existed.

    Returns whether the schema is known to be applied. Callers that need data
    immediately should let their own query raise ``PlatformStoreUnavailable``
    with a specific message; this only covers the bookkeeping step.
    """
    requested = {migration.version for migration in migrations}
    if requested <= _ready_versions:
        return True
    with _schema_lock:
        if requested <= _ready_versions:
            return True
        try:
            applied_now = apply_migrations(migrations)
        except PlatformStoreUnavailable as exc:
            logger.warning(
                "platform store: schema not applied (database unavailable): %s",
                exc.detail,
            )
            return False
        except Exception:  # noqa: BLE001 - startup must survive schema errors
            logger.exception("platform store: schema migration failed")
            return False
        # After a clean `apply_migrations`, every requested version is either
        # already recorded, just recorded, or was skipped as incomplete. Only the
        # first two are ready — marking a skipped one ready is what made a missing
        # table permanent and invisible (see `_incomplete_versions`).
        _ready_versions.update(requested - _incomplete_versions)
        return requested <= _ready_versions


def reset_schema_flag() -> None:
    """Forget the once-per-process bookkeeping (tests)."""
    global _ready_versions, _incomplete_versions
    with _schema_lock:
        _ready_versions = set()
        _incomplete_versions = set()


__all__ = [
    "Migration",
    "applied_versions",
    "apply_migrations",
    "ensure_schema",
    "reset_schema_flag",
]
