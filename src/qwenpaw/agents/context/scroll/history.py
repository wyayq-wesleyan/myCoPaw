# -*- coding: utf-8 -*-
"""Durable, file-backed conversation history shared across sessions."""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentscope.message import Msg

from ..types import LogEntry

logger = logging.getLogger(__name__)

_BUSY_TIMEOUT_MS = 5000
_UNSET = object()

# The recall tool's own turns — the model's ``ms.*`` Python source and its
# printed stdout/stderr — are written through to history like any turn, but
# they are the agent *reading* memory, not memory content. Keyword-indexing
# them lets a later ``ms.search`` match the agent's own past queries (and their
# tracebacks), drowning the real content: a self-pollution feedback loop. So
# these rows stay durable + recallable by ``seq``, but are kept OUT of the FTS
# index (and out of ``search`` — see ``MemorySpace``). Must match the recall
# tool names in ``repl.py`` and ``recall_tool.py``.
_RECALL_TOOL_NAMES = (
    "recall_history_python",
    "recall_history",
)

# Columns of conversation_history, in INSERT order (minus the
# autoincrement seq).
_INSERT_COLUMNS = (
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


class HistoryStore:
    """Owns the *read-write* connection to the ``conversation_history`` file.

    Every event the agent appends is write-through-persisted here with full
    structure (blocks, tool args, state) so a later session can retrieve it.
    The model reaches the same file *read-only* through its ``MemorySpace``
    (ATTACHed ``hist`` schema), so this writer and those readers coexist under
    WAL. The file is never dropped; ``close()`` only closes this connection.
    """

    # FTS5 is a property of the SQLite build, not of one DB — warn at most once
    # per process when it's missing, so a long-lived server doesn't log-spam.
    _fts_unavailable_warned = False

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Serializes the single connection across threads: ``compress`` writes
        # from a worker thread (``asyncio.to_thread``, to spare the event loop)
        # while ``on_save`` writes on the loop thread. Both share this
        # connection, so every access takes ``self._lock``.
        self._lock = threading.Lock()
        self.quarantined_to: Path | None = None
        # Durability health: flipped True the first time a write-through fails
        # (disk/SQLite error). The durability promise no longer holds while
        # degraded; callers/monitoring can read this.
        self.degraded = False
        self.write_failures = 0
        # Flipped True by ``close()`` so callers can tell an intentional
        # teardown race from a real disk outage (see ``closed``).
        self._closed = False
        try:
            self._open_and_init()
        except sqlite3.DatabaseError as exc:
            # A corrupt / unreadable DB (truncated file, stale WAL trio, bad
            # page) would crash every task at startup. Quarantine the bad file
            # and recreate fresh, degrading "broken memory" to "lost history".
            self._quarantine(exc)
            self._open_and_init()

    def _open_and_init(self) -> None:
        # check_same_thread=False: used from both loop and worker threads;
        # ``self._lock`` provides the serialization SQLite would get from
        # same-thread affinity.
        self._conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        # Probe for corruption that only surfaces on read.
        row = self._conn.execute("PRAGMA quick_check").fetchone()
        if not row or row[0] != "ok":
            raise sqlite3.DatabaseError(
                f"quick_check failed: {row[0] if row else None}",
            )
        self._init_schema()

    def _quarantine(self, exc: Exception) -> None:
        """Move the unreadable DB + its -wal/-shm aside with a timestamp."""
        try:
            self._conn.close()
        except (AttributeError, sqlite3.Error):
            pass
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(self._path) + suffix)
            if not src.exists():
                continue
            dest = Path(f"{self._path}.corrupt-{ts}{suffix}")
            try:
                src.rename(dest)
                if suffix == "":
                    self.quarantined_to = dest
            except OSError:
                try:
                    src.unlink()  # last resort so a fresh DB can be created
                except OSError:
                    pass
        print(
            f"[HistoryStore] {self._path} was unreadable ({exc}); quarantined "
            f"to {self.quarantined_to} and recreated a fresh store.",
            file=sys.stderr,
        )

    @property
    def path(self) -> Path:
        return self._path

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_history (
                    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id   TEXT NOT NULL,
                    agent_id     TEXT,
                    kind         TEXT NOT NULL,
                    role         TEXT,
                    name         TEXT,
                    content      TEXT,
                    tool_call_id TEXT,
                    tool_input   TEXT,
                    tool_state   TEXT,
                    headline     TEXT,
                    blocks       TEXT,
                    metadata     TEXT,
                    created_at   TEXT,
                    dedup_key    TEXT
                )
                """,
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS ch_session "
                "ON conversation_history(session_id)",
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS ch_agent "
                "ON conversation_history(agent_id)",
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS ch_kind "
                "ON conversation_history(kind)",
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS ch_created_at "
                "ON conversation_history(created_at)",
            )
            # Idempotency net: a second append of the same logical event, such
            # as a resume re-persisting its restored window, collides here and
            # is dropped by ON CONFLICT rather than duplicating a row. NULL
            # dedup_key never conflicts, so un-keyed rows are simply never
            # deduped.
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_dedup "
                "ON conversation_history(session_id, dedup_key)",
            )
            self._init_fts()

    def _init_fts(self) -> None:
        """Create the FTS5 full-text index over ``content``, if available.

        External-content FTS5 indexes without duplicating the text; it is kept
        in sync by ``append``/``update_entry``. On a pre-existing DB it is
        back-filled once via 'rebuild'. Porter stemming on top of unicode61
        casefolding so "tanks" matches "tank".

        Attempting the ``CREATE VIRTUAL TABLE`` is itself the availability
        probe: SQLite builds without the FTS5 module (some minimal
        container/distro builds) raise ``no such module: fts5`` here. We catch
        that, leave ``self._fts`` False so the write path skips FTS upkeep, and
        log one warning — search then degrades to a LIKE scan (see
        ``MemorySpace.search``). The store itself stays fully functional.
        """
        try:
            existed = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='conversation_history_fts'",
            ).fetchone()
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS conversation_history_fts "
                "USING fts5(content, content='conversation_history', "
                "content_rowid='seq', tokenize='porter unicode61')",
            )
            if not existed:
                self._conn.execute(
                    "INSERT INTO conversation_history_fts"
                    "(conversation_history_fts) VALUES('rebuild')",
                )
            self._fts = True
        except sqlite3.OperationalError as exc:
            self._fts = False
            if not HistoryStore._fts_unavailable_warned:
                HistoryStore._fts_unavailable_warned = True
                logger.warning(
                    "SQLite has no FTS5 module (%s); scroll history keyword "
                    "search degrades to a slower LIKE scan. The history store "
                    "is otherwise fully functional. Use a SQLite build with "
                    "FTS5 to restore ranked full-text recall.",
                    exc,
                )

    # --- write path ----------------------------------------------------

    @staticmethod
    def _insert_row(
        session_id: str,
        agent_id: str | None,
        entry: LogEntry,
        dedup_key: str | None,
    ) -> tuple:
        """Build the SQLite row shared by single and batched appends."""
        return (
            session_id,
            agent_id,
            entry.kind,
            entry.role,
            entry.name,
            entry.content,
            entry.tool_call_id,
            _to_json(entry.tool_input),
            entry.tool_state,
            entry.headline,
            _to_json(entry.blocks),
            _to_json(entry.metadata or None),
            entry.created_at or datetime.now(timezone.utc).isoformat(),
            dedup_key,
        )

    def append(
        self,
        *,
        session_id: str,
        entry: LogEntry,
        agent_id: str | None = None,
        dedup_key: str | None = None,
    ) -> int:
        """Write-through one event. Returns the assigned ``seq`` (watermark).

        ``dedup_key`` is the row's stable identity within the session (the
        source ``msg.id`` for a turn, the ``tool_call_id`` for a result). A
        second append carrying the same ``(session_id, dedup_key)`` is a no-op
        and returns the *existing* seq, so a resume that re-persists its
        restored window can re-link bookkeeping without duplicating rows. A
        ``None`` key is never deduped.
        """
        row = self._insert_row(session_id, agent_id, entry, dedup_key)
        placeholders = ", ".join("?" for _ in _INSERT_COLUMNS)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"INSERT INTO conversation_history "
                f"({', '.join(_INSERT_COLUMNS)}) VALUES ({placeholders}) "
                f"ON CONFLICT(session_id, dedup_key) DO NOTHING",
                row,
            )
            if cur.rowcount == 0:
                # Conflict: this event is already durable. Return its seq so
                # the caller re-links to the existing row; no new row, no FTS
                # write.
                existing = self._conn.execute(
                    "SELECT seq FROM conversation_history "
                    "WHERE session_id = ? AND dedup_key = ?",
                    (session_id, dedup_key),
                ).fetchone()
                return int(existing["seq"]) if existing else 0
            seq = int(cur.lastrowid or 0)
            if self._fts and entry.name not in _RECALL_TOOL_NAMES:
                self._conn.execute(
                    "INSERT INTO conversation_history_fts(rowid, content) "
                    "VALUES (?, ?)",
                    (seq, entry.content or ""),
                )
            return seq

    def append_many(
        self,
        *,
        session_id: str,
        entries: Sequence[tuple[LogEntry, str | None]],
        agent_id: str | None = None,
    ) -> int:
        """Append a group of events in one transaction.

        Returns the number of newly inserted rows. Duplicate keys remain
        no-ops and only newly inserted rows are added to FTS, matching
        :meth:`append`. This is intended for backfills where committing every
        individual row would turn SQLite fsync latency into startup latency.
        """
        if not entries:
            return 0

        placeholders = ", ".join("?" for _ in _INSERT_COLUMNS)
        sql = (
            f"INSERT INTO conversation_history "
            f"({', '.join(_INSERT_COLUMNS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(session_id, dedup_key) DO NOTHING"
        )
        inserted = 0
        with self._lock, self._conn:
            for entry, dedup_key in entries:
                row = self._insert_row(
                    session_id,
                    agent_id,
                    entry,
                    dedup_key,
                )
                cur = self._conn.execute(sql, row)
                if cur.rowcount == 0:
                    continue
                inserted += 1
                seq = int(cur.lastrowid or 0)
                if self._fts and entry.name not in _RECALL_TOOL_NAMES:
                    self._conn.execute(
                        "INSERT INTO conversation_history_fts(rowid, content) "
                        "VALUES (?, ?)",
                        (seq, entry.content or ""),
                    )
        return inserted

    def update_entry(
        self,
        seq: int,
        *,
        content: str | None,
        headline: str | None,
        blocks,
        tool_call_id: str | None = None,
        name: str | None = None,
        tool_state: str | None = None,
        tool_input: Any = None,
        metadata: Any = _UNSET,
    ) -> None:
        """Refresh an already-appended row in place (keeping FTS in sync).

        Used when one logical turn is *extended* after first write: AgentScope
        accumulates a whole reply into a single assistant Msg, so the durable
        row must end up with every cell's blocks and any later-emitted
        headline. The scalar ``tool_call_id``/``name``/``tool_state``/
        ``tool_input`` and message ``metadata`` are refreshed too, so a turn
        that grows a *later* tool call doesn't leave them frozen at their
        first-write values. ``seq`` is unchanged. Omitting ``metadata`` keeps
        the stored value unchanged for backwards-compatible direct callers.
        """
        # Recall-tool rows are never FTS-indexed (see ``_RECALL_TOOL_NAMES``),
        # so don't touch the index for them on update either.
        fts_sync = self._fts and name not in _RECALL_TOOL_NAMES
        with self._lock, self._conn:
            old_content = None
            if fts_sync:
                r = self._conn.execute(
                    "SELECT content FROM conversation_history WHERE seq = ?",
                    (seq,),
                ).fetchone()
                old_content = r["content"] if r else None
            # Keep every column name below as a hard-coded literal. Only
            # values are parameterized; never add caller-controlled names.
            assignments = [
                "content = ?",
                "headline = ?",
                "blocks = ?",
                "tool_call_id = ?",
                "name = ?",
                "tool_state = ?",
                "tool_input = ?",
            ]
            values: list[Any] = [
                content,
                headline,
                _to_json(blocks),
                tool_call_id,
                name,
                tool_state,
                _to_json(tool_input),
            ]
            if metadata is not _UNSET:
                assignments.append("metadata = ?")
                # The history schema canonically represents empty metadata
                # as SQL NULL, matching the initial insert path.
                values.append(_to_json(metadata or None))
            values.append(seq)
            self._conn.execute(
                "UPDATE conversation_history SET "
                + ", ".join(assignments)
                + " WHERE seq = ?",
                values,
            )
            if fts_sync:
                if old_content is not None:
                    self._conn.execute(
                        "INSERT INTO conversation_history_fts"
                        "(conversation_history_fts, rowid, content) "
                        "VALUES('delete', ?, ?)",
                        (seq, old_content),
                    )
                self._conn.execute(
                    "INSERT INTO conversation_history_fts(rowid, content) "
                    "VALUES (?, ?)",
                    (seq, content or ""),
                )

    # --- read path -----------------------------------------------------

    def count(self, session_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) AS n FROM conversation_history "
                "WHERE session_id = ?",
                (session_id,),
            )
            return int(cur.fetchone()["n"])

    def load_session_messages(
        self,
        session_id: str,
        agent_id: str | None = None,
    ) -> list[Msg]:
        """Rebuild this session's full transcript, oldest first.

        The live context window only holds what Scroll has not evicted, so it
        is *not* the conversation: reading it as the transcript silently
        dropped every evicted turn (the anchor list shrank while chatting, and
        "jump to top" landed in the middle of the session). These rows are the
        durable record the model itself recalls from, so they are the correct
        source for a transcript view.

        ``blocks`` holds each block's ``model_dump``; AgentScope coerces those
        dicts back into typed blocks by their ``type`` discriminator, so tool
        calls and media survive rather than degrading to plain text. A row that
        cannot be rebuilt is skipped instead of failing the whole read — a
        partially readable transcript beats none.
        """
        if not session_id:
            return []
        where = ["session_id = ?"]
        params: list[Any] = [session_id]
        if agent_id:
            # Early migration versions wrote agent_id=NULL. Session plus seq
            # range already pins those rows here, so keep them.
            where.append("(agent_id = ? OR agent_id IS NULL)")
            params.append(agent_id)
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, kind, role, name, content, blocks, metadata, "
                "created_at "
                "FROM conversation_history "
                "WHERE " + " AND ".join(where) +
                # Order by the time the turn happened, NOT by ``seq``.
                #
                # ``seq`` is the *write* order, and it does not always match
                # the conversation's order: a turn can be persisted later than
                # the turn that follows it (a resumed/retried turn, or rows
                # imported by the session-sync pass), which made the console
                # render a question *below* its answer after a refresh. The
                # live context window was unaffected, which is why this only
                # appeared once the transcript came from the durable store.
                #
                # ``seq`` remains the tie-break: every row of one turn shares
                # its message's ``created_at`` (the serializer stamps them all
                # from the same Msg), so the within-turn order — tool call
                # before its result — is still the write order.
                #
                # ``created_at IS NULL`` is sorted on first so a row with no
                # timestamp falls *last* (keeping its seq relative to other
                # undated rows) instead of jumping to the head of the
                # conversation, which is where SQLite puts NULLs by default.
                " ORDER BY (created_at IS NULL), created_at, seq",
                tuple(params),
            ).fetchall()

        messages: list[Msg] = []
        for row in rows:
            msg = _row_to_msg(row)
            if msg is not None:
                messages.append(msg)
        return messages

    def claim_session(self, session_id: str, agent_id: str | None) -> int:
        """Assign legacy unowned rows in a canonical session to an agent."""
        if not session_id or not agent_id:
            return 0
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE conversation_history SET agent_id = ? "
                "WHERE session_id = ? AND agent_id IS NULL",
                (agent_id, session_id),
            )
            return int(cur.rowcount)

    def reconcile_session_rows(
        self,
        source_ids: set[str],
        target_id: str,
        dedup_keys: set[str],
        *,
        agent_id: str | None = None,
    ) -> tuple[int, int, int]:
        """Move rows proven to come from one file into its canonical session.

        ``source_ids`` alone is not sufficient provenance because synthetic
        IDs can collide across channel directories. Restricting the operation
        to dedup keys recomputed from the source file prevents unrelated rows
        from being swept into ``target_id``.

        Returns ``(moved, deduplicated, claimed)``. Non-conflicting rows retain
        their original ``seq``; source duplicates are removed in favor of the
        existing canonical row so the unique dedup contract remains valid.
        """
        sources = sorted(
            source_id
            for source_id in source_ids
            if source_id and source_id != target_id
        )
        keys = sorted(str(key) for key in dedup_keys if key)
        if not sources or not target_id or not keys:
            return (0, 0, self.claim_session(target_id, agent_id))

        moved = 0
        deduplicated = 0
        claimed = 0
        with self._lock, self._conn:
            for source_id in sources:
                for start in range(0, len(keys), 400):
                    chunk = keys[slice(start, start + 400)]
                    placeholders = ", ".join("?" for _ in chunk)
                    ownership = ""
                    params: list[Any] = [source_id, *chunk]
                    if agent_id:
                        ownership = " AND (agent_id = ? OR agent_id IS NULL)"
                        params.append(agent_id)
                    rows = self._conn.execute(
                        "SELECT seq, dedup_key, content "
                        "FROM conversation_history "
                        "WHERE session_id = ? AND dedup_key IN ("
                        + placeholders
                        + ")"
                        + ownership,
                        params,
                    ).fetchall()
                    if not rows:
                        continue

                    row_keys = [str(row["dedup_key"]) for row in rows]
                    target_placeholders = ", ".join("?" for _ in row_keys)
                    existing = self._conn.execute(
                        "SELECT dedup_key FROM conversation_history "
                        "WHERE session_id = ? AND dedup_key IN ("
                        + target_placeholders
                        + ")",
                        [target_id, *row_keys],
                    ).fetchall()
                    existing_keys = {str(row["dedup_key"]) for row in existing}
                    duplicates = [
                        row
                        for row in rows
                        if str(row["dedup_key"]) in existing_keys
                    ]
                    movable = [
                        row
                        for row in rows
                        if str(row["dedup_key"]) not in existing_keys
                    ]

                    if self._fts:
                        for row in duplicates:
                            self._conn.execute(
                                "INSERT INTO conversation_history_fts"
                                "(conversation_history_fts, rowid, content) "
                                "VALUES('delete', ?, ?)",
                                (row["seq"], row["content"] or ""),
                            )
                    if duplicates:
                        self._conn.executemany(
                            "DELETE FROM conversation_history WHERE seq = ?",
                            [(row["seq"],) for row in duplicates],
                        )
                        deduplicated += len(duplicates)

                    if movable:
                        seqs = [int(row["seq"]) for row in movable]
                        seq_placeholders = ", ".join("?" for _ in seqs)
                        cur = self._conn.execute(
                            "UPDATE conversation_history "
                            "SET session_id = ?, "
                            "agent_id = COALESCE(agent_id, ?) "
                            "WHERE seq IN (" + seq_placeholders + ")",
                            [target_id, agent_id, *seqs],
                        )
                        moved += int(cur.rowcount)

            if agent_id:
                cur = self._conn.execute(
                    "UPDATE conversation_history SET agent_id = ? "
                    "WHERE session_id = ? AND agent_id IS NULL",
                    (agent_id, target_id),
                )
                claimed = int(cur.rowcount)
        return (moved, deduplicated, claimed)

    def existing_seqs(self, seqs: set[int]) -> set[int]:
        """Return the subset of globally addressed history rows that exist."""
        if not seqs:
            return set()
        ordered = sorted(int(seq) for seq in seqs)
        placeholders = ", ".join("?" for _ in ordered)
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq FROM conversation_history WHERE seq IN ("
                + placeholders
                + ")",
                ordered,
            ).fetchall()
        return {int(row["seq"]) for row in rows}

    def contents_by_seqs(self, seqs: set[int]) -> dict[int, str | None]:
        """Return exact persisted content for globally addressed rows.

        Summary evidence uses this after live tool results have been folded.
        Querying exact primary keys, instead of a broad ``lo..hi`` range,
        prevents interleaved rows from another agent or session entering the
        evidence. Chunking also keeps the query below SQLite parameter limits
        for unusually tool-heavy histories.
        """
        if not seqs:
            return {}
        ordered = sorted(int(seq) for seq in seqs)
        found: dict[int, str | None] = {}
        with self._lock:
            for start in range(0, len(ordered), 500):
                chunk = ordered[start : start + 500]
                placeholders = ", ".join("?" for _ in chunk)
                rows = self._conn.execute(
                    "SELECT seq, content FROM conversation_history "
                    f"WHERE seq IN ({placeholders})",
                    chunk,
                ).fetchall()
                found.update(
                    {int(row["seq"]): row["content"] for row in rows},
                )
        return found

    @staticmethod
    def _purge_where(
        before: str,
        kinds: tuple[str, ...] | None,
    ) -> tuple[str, list]:
        """Build the shared ``WHERE`` for purge/estimate so they can't drift.

        Always bounds by ``created_at < before`` (NULL ``created_at`` is never
        matched, so unstamped rows are retained). When ``kinds`` is given, also
        restricts to those row kinds (e.g. ``("tool_result",)`` to drop only
        tool output and keep the conversation). Values are bound, never
        interpolated.
        """
        clause = "created_at IS NOT NULL AND created_at < ?"
        params: list = [before]
        if kinds:
            placeholders = ", ".join("?" for _ in kinds)
            clause += f" AND kind IN ({placeholders})"
            params.extend(kinds)
        return clause, params

    def estimate_purge(
        self,
        *,
        before: str,
        kinds: tuple[str, ...] | None = None,
    ) -> dict:
        """How much ``purge(before=...)`` would remove — WITHOUT removing it.

        Returns ``{"rows": n, "content_bytes": b}`` where ``content_bytes`` is
        the summed length of the ``content`` column for the matched rows (the
        bulk of the on-disk weight; the FTS index roughly mirrors it, so true
        reclaim is larger). ``kinds`` narrows to specific row kinds (e.g.
        ``("tool_result",)`` to size only tool output). A dry-run estimate to
        show before an operator commits a purge, so they never delete blindly.
        """
        where, params = self._purge_where(before, kinds)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COUNT(*) AS rows, "
                "COALESCE(SUM(LENGTH(content)), 0) AS content_bytes "
                "FROM conversation_history WHERE " + where,
                params,
            ).fetchone()
        return {
            "rows": int(row["rows"]),
            "content_bytes": int(row["content_bytes"]),
        }

    def purge(
        self,
        *,
        before: str,
        dry_run: bool = False,
        kinds: tuple[str, ...] | None = None,
    ) -> int:
        """Delete history rows with ``created_at < before`` (ISO-8601).

        Returns the number of rows that match (and, unless ``dry_run``, were
        removed). With ``dry_run=True`` nothing is deleted — the count is
        computed and returned so a caller can preview the blast radius (pair
        with :meth:`estimate_purge` for the byte estimate). ``kinds`` narrows
        the delete to specific row kinds — e.g. ``("tool_result",)`` drops only
        tool output (the bulk of the bloat) while keeping the conversation
        turns. The FTS index is kept in sync (each purged row is removed from
        it first). Rows with a NULL/empty ``created_at`` are never matched, so
        they are retained. This is the retention/clear path — driven on
        startup and teardown by ``history_retention_days`` (default 30; set 0
        to keep history forever, which calls nothing here).

        Note: this DELETEs but does not ``VACUUM``, so freed pages are reused
        but the file does not shrink on disk until a separate vacuum.
        """
        where, params = self._purge_where(before, kinds)
        with self._lock, self._conn:
            doomed = self._conn.execute(
                "SELECT seq, content FROM conversation_history WHERE " + where,
                params,
            ).fetchall()
            if not doomed:
                return 0
            if dry_run:
                return len(doomed)
            if self._fts:
                for row in doomed:
                    self._conn.execute(
                        "INSERT INTO conversation_history_fts"
                        "(conversation_history_fts, rowid, content) "
                        "VALUES('delete', ?, ?)",
                        (row["seq"], row["content"] or ""),
                    )
            self._conn.execute(
                "DELETE FROM conversation_history WHERE " + where,
                params,
            )
            return len(doomed)

    def vacuum(self) -> None:
        """Rebuild the database file to reclaim space freed by ``purge``.

        ``purge`` only DELETEs rows, so freed pages are reused but the file
        does not shrink on disk. VACUUM rewrites it compactly. It is O(db size)
        and briefly needs extra scratch space, so it is an explicit, separate
        step rather than run inline on the retention purge path.
        """
        # VACUUM cannot run inside an open transaction; sqlite3 in its default
        # isolation mode opens one implicitly on writes, so commit first.
        with self._lock:
            self._conn.commit()
            self._conn.execute("VACUUM")

    def note_write_failure(self, exc: BaseException) -> None:
        """Record a write-through failure — durability is now degraded.

        Logs prominently on the first failure (then counts the rest, to avoid
        log spam). Read ``degraded`` to gate any "fully durable" guarantees.
        """
        self.write_failures += 1
        if not self.degraded:
            self.degraded = True
            logger.error(
                "history write-through FAILED; durability degraded "
                "(further failures counted silently): %s",
                exc,
            )

    @property
    def closed(self) -> bool:
        """True once :meth:`close` has run — the connection is gone."""
        return self._closed

    def close(self) -> None:
        self._closed = True
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def __repr__(self) -> str:
        return f"<HistoryStore path={self._path}>"


def _to_json(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def _loads(value: Any) -> Any:
    """Decode a JSON column, returning ``None`` when it is absent or broken."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


_BLOCK_TYPES: dict[str, Any] | None = None

# Transcript payload budget for one tool result.
#
# Returning the whole durable transcript (instead of only the live window) is
# required for navigation, but tool outputs are unbounded: it is normal for one
# result to hold tens of KB, so 500 such turns produced a 19MB response and a
# 1000-turn session tens of MB — which the console then had to download and
# parse. The transcript view only needs enough to recognise the call; the full
# text stays in ``history.db`` and is recoverable through the recall tools.
_TRANSCRIPT_TOOL_RESULT_MAX_CHARS = 4000
_TRANSCRIPT_TRUNCATED_MARK = "\n\n…[内容过长已截断，完整内容可用 recall 工具获取]"


def _truncate_tool_result(block: Any) -> Any:
    """Cap one tool result's rendered output for the transcript view.

    ``output`` is either a string or a list of blocks; both shapes are bounded
    so no single result can dominate the response.
    """
    limit = _TRANSCRIPT_TOOL_RESULT_MAX_CHARS
    output = getattr(block, "output", None)
    if isinstance(output, str):
        if len(output) > limit:
            block.output = output[:limit] + _TRANSCRIPT_TRUNCATED_MARK
        return block
    if isinstance(output, list):
        for item in output:
            text = (
                item.get("text")
                if isinstance(item, dict)
                else getattr(item, "text", None)
            )
            if not isinstance(text, str) or len(text) <= limit:
                continue
            truncated = text[:limit] + _TRANSCRIPT_TRUNCATED_MARK
            if isinstance(item, dict):
                item["text"] = truncated
            else:
                try:
                    item.text = truncated
                except Exception:  # noqa: BLE001 - frozen block, leave as is
                    pass
    return block


def _coerce_block(item: Any) -> Any:
    """Turn one stored block dict into its typed AgentScope block.

    ``Msg.content`` is a union whose **first** member is ``TextBlock``, and
    pydantic does not fall through to the other members once a member's literal
    ``type`` fails — so handing ``Msg`` raw dicts rejects every non-text block.
    Resolving the concrete class by ``type`` first keeps tool calls, tool
    results, thinking and media intact instead of degrading a turn to text.

    Tool results are additionally capped: see ``_TRANSCRIPT_TOOL_RESULT_MAX_CHARS``.
    """
    global _BLOCK_TYPES
    if not isinstance(item, dict):
        return item
    if _BLOCK_TYPES is None:
        from agentscope.message import (
            DataBlock,
            HintBlock,
            TextBlock,
            ThinkingBlock,
            ToolCallBlock,
            ToolResultBlock,
        )

        _BLOCK_TYPES = {
            "text": TextBlock,
            "thinking": ThinkingBlock,
            "hint": HintBlock,
            "tool_call": ToolCallBlock,
            "tool_result": ToolResultBlock,
            "data": DataBlock,
        }
    block_type = str(item.get("type") or "")
    block_cls = _BLOCK_TYPES.get(block_type)
    if block_cls is None:
        logger.debug("scroll: unknown block type %r dropped", block_type)
        return None
    try:
        block = block_cls.model_validate(item)
    except Exception:  # noqa: BLE001 - skip a block rather than the whole row
        logger.debug(
            "scroll: could not rebuild %s block", block_type,
            exc_info=True,
        )
        return None
    if block_type == "tool_result":
        block = _truncate_tool_result(block)
    return block


def _row_to_msg(row: sqlite3.Row) -> Msg | None:
    """Rebuild one durable row into a ``Msg`` for the transcript view.

    ``blocks`` is the faithful copy of the turn's content (each block's
    ``model_dump``), so it wins over the flattened ``content`` text, which has
    already lost tool calls and media. A row with no usable content — or one
    that fails to parse at all — is dropped rather than raising, so a single
    bad row cannot blank an entire conversation.
    """
    from agentscope.message import Msg

    role = (row["role"] or "assistant").strip() or "assistant"
    raw_blocks = _loads(row["blocks"])
    content: list[Any] = []
    if isinstance(raw_blocks, list):
        content = [
            block
            for block in (_coerce_block(item) for item in raw_blocks)
            if block is not None
        ]
    if not content:
        fallback = _coerce_block({"type": "text", "text": row["content"] or ""})
        if fallback is None:
            return None
        content = [fallback]
    metadata = _loads(row["metadata"]) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    # The console reads a message's time from ``metadata.timestamp`` (see
    # ``sessionApi.parseTimestamp``), and `Msg.timestamp` aliases
    # ``created_at``. Without restoring it here, every rebuilt message had no
    # time at all, so the frontend could not order a turn's request against its
    # reply — after a refresh the question rendered *below* the answer.
    created_at = row["created_at"] if "created_at" in row.keys() else None
    if created_at and "timestamp" not in metadata:
        metadata["timestamp"] = created_at
    try:
        # ``Msg.created_at`` rejects None, so it is passed only when present.
        fields: dict[str, Any] = {
            "name": row["name"] or role,
            "role": role,
            "content": content,
            "metadata": metadata,
        }
        if created_at:
            fields["created_at"] = created_at
        return Msg(**fields)
    except Exception:  # noqa: BLE001 - one bad row must not break the read
        logger.debug(
            "scroll: could not rebuild history row seq=%s", row["seq"],
            exc_info=True,
        )
        return None
