# -*- coding: utf-8 -*-
"""AuditLog — Audit records for each assert_policy + audit call.

Storage: the platform's PostgreSQL store (``platform.audit_events``), global
singleton.

* record() writes immediately, no in-memory buffer
* query() supports filtering by workspace / agent / tool / decision / time range,
  with pagination
* purge() deletes expired records
* Auto-cleanup: when total records reach 100k, deletes the oldest 10k

**Why this moved off SQLite.** It used to be one global ``audit.db`` file shared
by every account: isolation existed only as a column in the row, so one tenant's
policy decisions sat in a file every process could read. It now lives in the
platform database alongside the rest of the platform's own data, where the same
rows can be filtered per workspace and an operator has one database to back up.

The public API is unchanged, including the singleton accessor and the
record-a-failure-never-propagates contract: an audit write must never disrupt the
policy decision returned to the caller.
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import psycopg2
import psycopg2.extras

from ..platform_store import (
    PlatformStoreUnavailable,
    dict_cursor,
    ensure_schema,
    get_conn,
    put_conn,
)
from ..platform_store.migrations import AUDIT_MIGRATIONS

from .policy import GovernanceDecision, ToolCallSpec

_logger = logging.getLogger(__name__)


def _now_unix_ms() -> int:
    """Return current UTC timestamp in milliseconds since epoch."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


@dataclass
class AuditEvent:
    """A single audit record.

    Records 5W: who (agent_id), what (tool_name + target),
    when (ts), outcome (decision), why (reason).
    """

    ts: int  # Milliseconds since epoch, UTC
    workspace_dir: str
    agent_id: str
    session_id: str
    tool_name: str
    target: str
    decision: str  # "allow" | "deny" | "ask" | "sandbox_fallback"
    reason: str = ""  # Additional explanation (e.g. violation cause)
    extra: dict = field(default_factory=dict)


def _event_from_row(row) -> AuditEvent:
    """Construct an AuditEvent from a database row."""
    raw_extra = row["extra"]
    if isinstance(raw_extra, str):
        try:
            extra = json.loads(raw_extra)
        except (TypeError, ValueError):
            extra = {}
    else:
        extra = raw_extra or {}
    return AuditEvent(
        ts=int(row["ts"]),
        workspace_dir=row["workspace_dir"] or "",
        agent_id=row["agent_id"] or "",
        session_id=row["session_id"] or "",
        tool_name=row["tool_name"] or "",
        target=row["target"] or "",
        decision=row["decision"] or "",
        reason=row["reason"] or "",
        extra=extra,
    )


class AuditLog:
    """Append-only audit log in PostgreSQL, global singleton.

    Shared by multiple ResourceGovernor instances; each ``audit()`` call
    (typically after ``assert_policy()``) invokes ``record()``, which writes
    immediately.

    .. note:: Threading

        Writes are synchronous and serialized by a ``threading.RLock``. A
        connection is checked out per operation and returned immediately, so the
        pool is never held across an idle period.
    """

    MAX_RECORDS = 100_000  # Threshold to trigger auto-cleanup
    PURGE_COUNT = 10_000  # Number of records to delete per cleanup
    _CHECK_INTERVAL = 1_000

    _instance: Optional[AuditLog] = None
    _instance_lock = threading.Lock()
    _db_path: Path
    _insert_count: int
    _lock: threading.RLock

    @classmethod
    def get_instance(
        cls,
        db_dir: Optional[Path] = None,
    ) -> AuditLog:
        """Get the global singleton, initializing on first call.

        Args:
            db_dir: Retained for call-site compatibility. The audit table lives
                in the platform database now, so there is no per-directory file
                to select; the argument no longer changes where records go.
        """
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls._create(db_dir)
            return cls._instance

    @classmethod
    def close_instance(cls) -> None:
        """Reset the process-wide singleton.

        Connections are pooled and returned per call, so there is nothing to
        close here; dropping the reference lets a later ``get_instance`` build a
        fresh one (tests rely on this).
        """
        with cls._instance_lock:
            instance = cls._instance
            cls._instance = None
        if instance is not None:
            instance.close()

    @classmethod
    def _create(cls, db_dir: Optional[Path] = None) -> AuditLog:
        """Internal factory: build the instance and ensure the table exists."""
        obj = object.__new__(cls)
        obj._db_path = Path(db_dir) if db_dir is not None else Path(".")
        obj._insert_count = 0
        obj._lock = threading.RLock()
        # Schema is process-global and applied once; failures are isolated per
        # statement inside the migration framework.
        ensure_schema(AUDIT_MIGRATIONS)
        return obj

    def close(self) -> None:
        """Reset the singleton.

        Takes the write lock first so closing cannot interleave with an in-flight
        ``record()``. There is no connection to release — one is checked out per
        operation — but the same lock still serializes the two, and a later
        ``record()`` on this instance keeps working (it checks out a fresh
        connection, matching the previous behaviour where the object stayed
        usable).
        """
        with self._lock:
            with AuditLog._instance_lock:
                if AuditLog._instance is self:
                    AuditLog._instance = None

    # --- writes --------------------------------------------------------

    def record(
        self,
        workspace_dir: str,
        tc_spec: ToolCallSpec,
        decision: GovernanceDecision,
    ) -> None:
        """Record a policy decision, writing to the database immediately.

        Errors are caught and logged: an audit-write failure must NOT propagate
        into ``assert_policy`` and disrupt the policy decision returned to the
        caller. That includes the database being unreachable — an audit gap is
        reported, never fatal.

        ``audit_level == "none"`` is handled by ``ResourceGovernor.audit()``
        before this method is called.
        """
        try:
            with self._lock:
                conn = get_conn()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO platform.audit_events "
                            "(ts, workspace_dir, agent_id, session_id, "
                            "tool_name, target, decision, reason, extra) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                            (
                                _now_unix_ms(),
                                workspace_dir,
                                tc_spec.agent_id,
                                tc_spec.session_id,
                                tc_spec.tool_name,
                                tc_spec.target,
                                str(decision.action.value),
                                decision.reason,
                                "{}",
                            ),
                        )
                    conn.commit()
                finally:
                    put_conn(conn)

                # Auto-cleanup check
                self._insert_count += 1
                if self._insert_count >= self._CHECK_INTERVAL:
                    self._insert_count = 0
                    if self.count >= self.MAX_RECORDS:
                        self._auto_purge()
        except PlatformStoreUnavailable as e:
            _logger.error(
                "AuditLog.record: platform database unavailable "
                "(tool=%s, target=%r): %s",
                tc_spec.tool_name,
                (tc_spec.target or "")[:120],
                e.detail,
            )
        except psycopg2.Error as e:
            _logger.error(
                "AuditLog.record: database error (tool=%s, target=%r): %s",
                tc_spec.tool_name,
                (tc_spec.target or "")[:120],
                e,
                exc_info=True,
            )

    def purge(self, before: int) -> int:
        """Delete records before the specified time.

        Args:
            before: Cutoff time (unix ms, UTC), exclusive

        Returns:
            Number of deleted records, or ``0`` on error. PostgreSQL reclaims the
            freed space itself, so there is no VACUUM step.
        """
        try:
            with self._lock:
                conn = get_conn()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM platform.audit_events WHERE ts < %s",
                            (before,),
                        )
                        deleted = int(cur.rowcount)
                    conn.commit()
                finally:
                    put_conn(conn)
                return deleted
        except (PlatformStoreUnavailable, psycopg2.Error) as e:
            _logger.error("AuditLog.purge: database error: %s", e, exc_info=True)
            return 0

    def _auto_purge(self) -> None:
        """Delete the oldest ``PURGE_COUNT`` records (caller holds ``_lock``)."""
        try:
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    # One statement so the cutoff cannot drift between reading the
                    # boundary row and deleting up to it.
                    cur.execute(
                        "DELETE FROM platform.audit_events WHERE id IN ("
                        "  SELECT id FROM platform.audit_events "
                        "  ORDER BY id ASC LIMIT %s"
                        ")",
                        (self.PURGE_COUNT,),
                    )
                    deleted = int(cur.rowcount)
                conn.commit()
            finally:
                put_conn(conn)
            if deleted:
                _logger.info(
                    "AuditLog: auto-purged %d oldest records.", deleted,
                )
        except (PlatformStoreUnavailable, psycopg2.Error) as e:
            _logger.error(
                "AuditLog._auto_purge: database error: %s", e, exc_info=True,
            )

    # --- reads ---------------------------------------------------------

    def query(
        self,
        workspace_dir: Optional[str] = None,
        agent_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        decision: Optional[str] = None,
        since: Optional[int] = None,
        until: Optional[int] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[AuditEvent], int]:
        """Query audit events with pagination.

        Returns ``(events, total)`` — the page and the total number of matching
        records. Returns ``([], 0)`` when the database is unavailable or a query
        fails, so callers (e.g. the Console UI) get a safe empty page rather than
        an unhandled exception.
        """
        clauses: list[str] = []
        params: list = []

        if workspace_dir:
            clauses.append("workspace_dir = %s")
            params.append(workspace_dir)
        if agent_id:
            clauses.append("agent_id = %s")
            params.append(agent_id)
        if tool_name:
            clauses.append("tool_name = %s")
            params.append(tool_name)
        if decision:
            clauses.append("decision = %s")
            params.append(decision)
        if since:
            clauses.append("ts >= %s")
            params.append(since)
        if until:
            clauses.append("ts <= %s")
            params.append(until)

        where = " WHERE " + " AND ".join(clauses) if clauses else ""

        try:
            with self._lock:
                conn = get_conn()
                try:
                    with dict_cursor(conn) as cur:
                        cur.execute(
                            f"SELECT COUNT(*) AS n FROM platform.audit_events{where}",
                            params,
                        )
                        total = int(cur.fetchone()["n"])

                        cur.execute(
                            f"SELECT * FROM platform.audit_events{where} "
                            "ORDER BY ts DESC, id DESC LIMIT %s OFFSET %s",
                            params + [limit, offset],
                        )
                        rows = cur.fetchall()
                    return [_event_from_row(r) for r in rows], total
                finally:
                    put_conn(conn)
        except (PlatformStoreUnavailable, psycopg2.Error) as e:
            _logger.error("AuditLog.query: database error: %s", e, exc_info=True)
            return [], 0

    @property
    def count(self) -> int:
        """Total number of records (0 when the database is unavailable)."""
        try:
            with self._lock:
                conn = get_conn()
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT COUNT(*) FROM platform.audit_events")
                        return int(cur.fetchone()[0])
                finally:
                    put_conn(conn)
        except (PlatformStoreUnavailable, psycopg2.Error):
            return 0


atexit.register(AuditLog.close_instance)
