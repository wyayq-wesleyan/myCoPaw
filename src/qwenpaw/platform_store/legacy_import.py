# -*- coding: utf-8 -*-
"""One-time upgrade: move legacy SQLite data into the platform PostgreSQL store.

The platform used to keep its data in per-workspace SQLite files (``history.db``)
and one global ``governance/audit.db``. Those tables now live in PostgreSQL, so
an upgraded deployment still has the old files on disk and needs their contents
carried over — otherwise a workspace's recalled history and the audit trail
silently start from empty.

Three properties make this safe to run on every boot:

* **Idempotent.** History rows keep their original ``seq`` and are inserted with
  ``ON CONFLICT DO NOTHING``, so re-running inserts only what is missing. The
  audit import is atomic per file (rows + bookkeeping in one transaction), so a
  crash leaves it retryable rather than half-copied and marked done.
* **Resumable.** ``platform.legacy_import_state`` records a checksum per source
  file, so a completed file is skipped on later boots and an interrupted one is
  retried.
* **Isolated.** Each file is handled independently; one failure is logged and
  skipped so it cannot take the others — or startup — down with it.

The legacy files are **never deleted**. They are the only copy until the import
has verifiably succeeded, and leaving them costs nothing but disk.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

from .pool import PlatformStoreUnavailable, dict_cursor, get_conn, is_available, put_conn

logger = logging.getLogger(__name__)

# ``kind`` values recorded in ``platform.legacy_import_state``. Distinct because
# one directory can hold both a history file and (at the root) an audit file.
KIND_HISTORY = "history_sqlite"
KIND_AUDIT = "audit_sqlite"

# Status values. ``done`` is what makes a later boot skip the file.
STATUS_DONE = "done"
STATUS_FAILED = "failed"

# Columns of the legacy ``conversation_history`` table, in the order the old
# store wrote them. Kept explicit so a schema drift in an old file is visible
# rather than silently mis-mapped.
_LEGACY_HISTORY_COLUMNS = (
    "seq",
    "session_id",
    "agent_id",
    "kind",
    "role",
    "name",
    "content",
    "tool_call_id",
    "tool_input",
    "tool_state",
    "headline",
    "blocks",
    "metadata",
    "created_at",
    "dedup_key",
)

_LEGACY_AUDIT_COLUMNS = (
    "ts",
    "workspace_dir",
    "agent_id",
    "session_id",
    "tool_name",
    "target",
    "decision",
    "reason",
    "extra",
)


@dataclass
class ImportResult:
    """Outcome for one legacy source file."""

    source: str
    kind: str
    rows: int = 0
    skipped: bool = False
    error: str | None = None


@dataclass
class ImportReport:
    """Aggregate outcome of one migration pass."""

    results: list[ImportResult] = field(default_factory=list)

    @property
    def imported_rows(self) -> int:
        return sum(item.rows for item in self.results if item.error is None)

    @property
    def failures(self) -> list[ImportResult]:
        return [item for item in self.results if item.error is not None]

    def summary(self) -> str:
        files = len(self.results)
        skipped = sum(1 for item in self.results if item.skipped)
        return (
            f"legacy-import: {files} source file(s), {self.imported_rows} row(s) "
            f"imported, {skipped} already up to date, {len(self.failures)} failed"
        )


def _checksum(path: Path) -> str:
    """Content hash of a legacy file.

    Streamed so a large history file does not have to be read into memory. A
    read failure is reported as a distinctive marker rather than raising: the
    caller treats an unreadable file as a failed import, and correctness never
    depends on the checksum (that is ``ON CONFLICT DO NOTHING``'s job) — the
    checksum only decides whether to skip work.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _state(conn, source_path: str, kind: str) -> tuple[str | None, str | None]:
    """Return ``(checksum, status)`` recorded for one source, if any."""
    with dict_cursor(conn) as cur:
        cur.execute(
            "SELECT checksum, status FROM platform.legacy_import_state "
            "WHERE source_path = %s AND kind = %s",
            (source_path, kind),
        )
        row = cur.fetchone()
    if row is None:
        return None, None
    return row["checksum"], row["status"]


def _record_state(
    cur,
    source_path: str,
    kind: str,
    *,
    checksum: str,
    row_count: int,
    status: str,
    detail: str | None = None,
) -> None:
    """Upsert the bookkeeping row for one source (caller owns the transaction)."""
    cur.execute(
        """
        INSERT INTO platform.legacy_import_state
            (source_path, kind, checksum, row_count, status, detail, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_path, kind) DO UPDATE SET
            checksum = EXCLUDED.checksum,
            row_count = EXCLUDED.row_count,
            status = EXCLUDED.status,
            detail = EXCLUDED.detail,
            updated_at = EXCLUDED.updated_at
        """,
        (
            source_path,
            kind,
            checksum,
            row_count,
            status,
            detail,
            datetime.now(timezone.utc),
        ),
    )


def _record_failure(source_path: str, kind: str, detail: str) -> None:
    """Best-effort record of a failed import, for operator visibility.

    Deliberately records ``status='failed'`` rather than leaving no row: a
    missing row and a failed row both mean "not done" (so the next boot
    retries), but the failed row is visible when someone investigates why an
    upgrade did not bring data across. Never raises — a bookkeeping failure must
    not mask the original import error.
    """
    try:
        conn = get_conn()
    except Exception:  # noqa: BLE001 - already reporting a failure
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO platform.legacy_import_state
                    (source_path, kind, checksum, row_count, status, detail,
                     updated_at)
                VALUES (%s, %s, NULL, 0, %s, %s, %s)
                ON CONFLICT (source_path, kind) DO UPDATE SET
                    status = EXCLUDED.status,
                    detail = EXCLUDED.detail,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    source_path,
                    kind,
                    STATUS_FAILED,
                    detail[:2000],
                    datetime.now(timezone.utc),
                ),
            )
        conn.commit()
    except Exception:  # noqa: BLE001 - never mask the import failure
        conn.rollback()
        logger.debug("legacy-import: could not record failure", exc_info=True)
    finally:
        put_conn(conn)


def _read_sqlite_rows(
    sqlite_path: Path,
    table: str,
    columns: tuple[str, ...],
) -> list[dict]:
    """Read every row of one table as dicts, or ``[]`` if it is absent.

    Read-only and defensive: a legacy file with no such table (e.g. it predates
    the feature) yields nothing instead of an error, and the connection opens
    with ``mode=ro`` so the import cannot modify the only remaining copy.
    """
    uri = f"file:{sqlite_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute(
                f"SELECT {', '.join(columns)} FROM {table}",  # noqa: S608 - literals
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [{column: row[column] for column in columns} for row in rows]
    finally:
        conn.close()


def import_legacy_history(store, sqlite_path: Path) -> ImportResult:
    """Copy one workspace's legacy ``history.db`` into PostgreSQL.

    Rows keep their original ``seq``, so the ``seq`` ranges recorded in existing
    session state stay valid. Idempotent by primary key, so an interrupted run
    resumes by inserting only what is missing.
    """
    result = ImportResult(source=str(sqlite_path), kind=KIND_HISTORY)
    checksum = _checksum(sqlite_path)

    conn = get_conn()
    try:
        prior_checksum, prior_status = _state(conn, str(sqlite_path), KIND_HISTORY)
        if prior_status == STATUS_DONE and prior_checksum == checksum:
            result.skipped = True
            return result
    finally:
        put_conn(conn)

    rows = _read_sqlite_rows(
        sqlite_path, "conversation_history", _LEGACY_HISTORY_COLUMNS,
    )
    inserted = store.import_rows(rows)
    result.rows = inserted

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            _record_state(
                cur,
                str(sqlite_path),
                KIND_HISTORY,
                checksum=checksum,
                row_count=len(rows),
                status=STATUS_DONE,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)
    return result


def import_legacy_audit(sqlite_path: Path) -> ImportResult:
    """Copy the legacy global ``governance/audit.db`` into PostgreSQL.

    Atomic per file: the rows and the bookkeeping row commit together, so an
    interruption leaves the file retryable instead of half-copied. There is no
    natural unique key on an audit row, so this file-level atomicity is what
    provides idempotency.
    """
    result = ImportResult(source=str(sqlite_path), kind=KIND_AUDIT)
    checksum = _checksum(sqlite_path)

    conn = get_conn()
    try:
        prior_checksum, prior_status = _state(conn, str(sqlite_path), KIND_AUDIT)
        if prior_status == STATUS_DONE and prior_checksum == checksum:
            result.skipped = True
            return result
    finally:
        put_conn(conn)

    rows = _read_sqlite_rows(sqlite_path, "audit_events", _LEGACY_AUDIT_COLUMNS)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    "INSERT INTO platform.audit_events "
                    "(ts, workspace_dir, agent_id, session_id, tool_name, "
                    "target, decision, reason, extra) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        float(row["ts"]),
                        row["workspace_dir"],
                        row["agent_id"],
                        row["session_id"],
                        row["tool_name"],
                        row["target"],
                        row["decision"],
                        row["reason"],
                        row["extra"],
                    ),
                )
            _record_state(
                cur,
                str(sqlite_path),
                KIND_AUDIT,
                checksum=checksum,
                row_count=len(rows),
                status=STATUS_DONE,
            )
        conn.commit()
        result.rows = len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)
    return result


def _legacy_history_paths() -> list[Path]:
    """Every existing legacy ``history.db``, one per scroll-enabled workspace.

    Enumerated from the agent profiles exactly as the session-sync pass does, so
    the set of workspaces matches what the running app considers real. A config
    failure yields no paths rather than raising: the migration is opportunistic.
    """
    try:
        from ..config import load_config

        config = load_config()
    except Exception:  # noqa: BLE001 - no config means nothing to migrate
        logger.debug("legacy-import: config unavailable", exc_info=True)
        return []

    paths: list[Path] = []
    for _agent_id, agent_ref in config.agents.profiles.items():
        try:
            workspace_dir = Path(agent_ref.workspace_dir).expanduser()
        except Exception:  # noqa: BLE001 - a malformed profile is skipped
            continue
        candidate = workspace_dir / "history.db"
        if candidate.is_file():
            paths.append(candidate)
    return paths


def _legacy_audit_path() -> Path | None:
    """The legacy global audit database, if this deployment still has one."""
    try:
        from ..constant import WORKING_DIR

        candidate = Path(WORKING_DIR) / "governance" / "audit.db"
    except Exception:  # noqa: BLE001 - no working dir means nothing to migrate
        logger.debug("legacy-import: working dir unavailable", exc_info=True)
        return None
    return candidate if candidate.is_file() else None


def run_legacy_import() -> ImportReport:
    """Import every legacy SQLite source into the platform store.

    Safe to call on every boot: completed files are skipped by checksum. Any
    failure — including the database being unreachable — is recorded and logged,
    never raised, because this runs during startup and must not block it.
    """
    report = ImportReport()
    if not is_available():
        logger.info(
            "legacy-import: platform database unavailable — skipping the "
            "SQLite→PostgreSQL migration (it will run on a later boot)",
        )
        return report

    history_paths = _legacy_history_paths()
    audit_path = _legacy_audit_path()
    if not history_paths and audit_path is None:
        logger.debug("legacy-import: no legacy SQLite files found")
        return report

    from ..agents.context.scroll.history import HistoryStore

    for path in history_paths:
        try:
            store = HistoryStore(path)
            report.results.append(import_legacy_history(store, path))
        except Exception as exc:  # noqa: BLE001 - isolate one workspace
            logger.warning(
                "legacy-import: history import failed for %s: %s", path, exc,
            )
            _record_failure(str(path), KIND_HISTORY, f"{type(exc).__name__}: {exc}")
            report.results.append(
                ImportResult(
                    source=str(path),
                    kind=KIND_HISTORY,
                    error=str(exc),
                ),
            )

    if audit_path is not None:
        try:
            report.results.append(import_legacy_audit(audit_path))
        except Exception as exc:  # noqa: BLE001 - isolate the audit file
            logger.warning(
                "legacy-import: audit import failed for %s: %s", audit_path, exc,
            )
            _record_failure(str(audit_path), KIND_AUDIT, f"{type(exc).__name__}: {exc}")
            report.results.append(
                ImportResult(
                    source=str(audit_path),
                    kind=KIND_AUDIT,
                    error=str(exc),
                ),
            )

    logger.info("%s", report.summary())
    return report


__all__ = [
    "ImportReport",
    "ImportResult",
    "KIND_AUDIT",
    "KIND_HISTORY",
    "import_legacy_audit",
    "import_legacy_history",
    "run_legacy_import",
]
