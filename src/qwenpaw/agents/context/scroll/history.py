# -*- coding: utf-8 -*-
"""Durable conversation history in PostgreSQL.

Every event the agent appends is written through to a PostgreSQL table with full
structure (blocks, tool args, state) so a later session can retrieve it.

**Why PostgreSQL rather than the per-workspace SQLite file this replaced.** The
platform now keeps its own data in one database instead of scattering it across
per-workspace files: one thing to operate and back up, queryable across
workspaces, and with index maintenance, atomicity and concurrency handled by the
server rather than by hand-written WAL and FTS upkeep.

**What stayed the same.** The public API is unchanged — ``append``,
``append_many``, ``update_entry``, ``count``, ``load_session_messages``,
``claim_session``, ``reconcile_session_rows``, ``existing_seqs``,
``contents_by_seqs``, ``estimate_purge``, ``purge``, ``vacuum``, ``close`` — so
the scroll manager, the session-sync pass and the chat API did not need to
change. Two contracts are load-bearing and are preserved exactly:

* ``seq`` remains a **per-workspace** watermark starting at 1. The eviction index
  persists ``seq`` ranges inside session state, and ``update_entry`` /
  ``existing_seqs`` / ``contents_by_seqs`` address rows by ``seq`` alone, so
  renumbering existing history would invalidate all of it. A per-workspace
  counter allocates it (see ``_alloc_seqs``) rather than a global sequence.

* ``(session_id, dedup_key)`` remains the idempotency key, and a NULL key is
  never deduped — so re-persisting a restored window is a no-op instead of a
  duplicate.

The workspace is identified by the same ``db_path`` the callers already pass
(``<workspace_dir>/history.db``). The file is neither created nor read; the path
is only hashed into a stable key, which keeps every call site working unchanged
and gives the legacy import a natural mapping from file to workspace.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentscope.message import Msg

from ....platform_store import (
    PlatformStoreUnavailable,
    dict_cursor,
    ensure_schema,
    get_conn,
    put_conn,
)
from ....platform_store.migrations import ALL_MIGRATIONS
from ....utils.text_safety import strip_nuls
from ..types import LogEntry

logger = logging.getLogger(__name__)

_UNSET = object()

# The recall tool's own turns — the model's ``ms.*`` Python source and its
# printed stdout/stderr — are written through to history like any turn, but they
# are the agent *reading* memory, not memory content. Keyword-indexing them lets
# a later ``ms.search`` match the agent's own past queries (and their
# tracebacks), drowning the real content: a self-pollution feedback loop. Search
# filters these out by name. ``recall_history_python`` is the removed REPL: its
# name is retained so rows written before the PostgreSQL migration stay
# excluded.
RECALL_TOOL_NAMES = (
    "recall_history_python",
    "recall_history",
)
# Backwards-compatible private alias (older imports used the underscore name).
_RECALL_TOOL_NAMES = RECALL_TOOL_NAMES

# Columns written by every insert, in order. ``seq`` and ``workspace_key`` are
# added by the store, not the caller.
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


def workspace_key_for(path: str | Path) -> str:
    """Stable key identifying one workspace's history.

    Derived from the resolved path so the same workspace always maps to the same
    key, and different workspaces never collide. Hashed rather than using the
    path directly: keys are stored on every row (a long path would bloat the
    table and its indexes) and a CJK account directory would otherwise produce an
    awkward key.
    """
    resolved = str(Path(path).expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:24]


class HistoryStore:
    """Conversation history for one workspace, stored in PostgreSQL."""

    def __init__(self, db_path: str | Path) -> None:
        # The path identifies the workspace; no file is created or opened.
        self._path = Path(db_path).expanduser()
        self._workspace_key = workspace_key_for(self._path)
        # Kept for API compatibility: SQLite could quarantine a corrupt file, but
        # a server-side table has nothing to quarantine (and no local file to
        # rename). Unavailability is reported through PlatformStoreUnavailable
        # instead, which the callers already handle as a degraded state.
        self.quarantined_to: Path | None = None
        # Durability health: flipped True the first time a write-through fails.
        # The durability promise no longer holds while degraded; callers and
        # monitoring can read this.
        self.degraded = False
        self.write_failures = 0
        self._closed = False
        # Schema is process-global; applying it here keeps construction simple
        # while the migration framework makes it a no-op after the first call.
        ensure_schema(ALL_MIGRATIONS)

    # --- identity ------------------------------------------------------

    @property
    def path(self) -> Path:
        """The workspace path this store is scoped to.

        Retained because callers pass it to the recall tools; it is an
        identifier, not an open file.
        """
        return self._path

    @property
    def workspace_key(self) -> str:
        """The key every row of this workspace is stored under."""
        return self._workspace_key

    # --- write path ----------------------------------------------------

    @staticmethod
    def _insert_row(
        session_id: str,
        agent_id: str | None,
        entry: LogEntry,
        dedup_key: str | None,
    ) -> tuple:
        """Build the row values shared by single and batched appends.

        NUL scrubbing happens *before* JSON encoding. ``json.dumps`` escapes a
        NUL into the ASCII sequence ``\\u0000``, which no string-level strip can
        see — so stripping after encoding let the NUL survive the round trip and
        reappear in the loaded text. Every structured value is cleaned first,
        then serialized, then the plain text columns are cleaned too.

        PostgreSQL text columns reject NUL outright, so without this one
        embedded NUL in tool output aborts the whole turn's persistence.
        """
        return (
            strip_nuls(session_id),
            strip_nuls(agent_id),
            entry.kind,
            strip_nuls(entry.role),
            strip_nuls(entry.name),
            strip_nuls(entry.content),
            strip_nuls(entry.tool_call_id),
            _to_json(strip_nuls(entry.tool_input)),
            strip_nuls(entry.tool_state),
            strip_nuls(entry.headline),
            _to_json(strip_nuls(entry.blocks)),
            _to_json(strip_nuls(entry.metadata or None)) if entry.metadata else None,
            entry.created_at or datetime.now(timezone.utc).isoformat(),
            strip_nuls(dedup_key),
        )

    def _alloc_seqs(self, cur, count: int) -> list[int]:
        """Reserve *count* consecutive ``seq`` values for this workspace.

        One statement, so concurrent writers cannot interleave and hand out the
        same number: the upsert increments the counter and returns the new value
        atomically. The counter stores the *last allocated* value, which makes
        the fresh-row case and the increment case expressible in one statement
        (a fresh row must yield ``count``, not ``count - 1``).
        """
        if count <= 0:
            return []
        cur.execute(
            """
            INSERT INTO platform.history_seq (workspace_key, last_seq)
            VALUES (%s, %s)
            ON CONFLICT (workspace_key)
            DO UPDATE SET last_seq = platform.history_seq.last_seq + %s
            RETURNING last_seq AS allocated
            """,
            (self._workspace_key, count, count),
        )
        last = int(cur.fetchone()["allocated"])
        return list(range(last - count + 1, last + 1))

    def append(
        self,
        *,
        session_id: str,
        entry: LogEntry,
        agent_id: str | None = None,
        dedup_key: str | None = None,
    ) -> int:
        """Write-through one event. Returns the assigned ``seq`` (watermark).

        ``dedup_key`` is the row's stable identity within the session (the source
        ``msg.id`` for a turn, the ``tool_call_id`` for a result). A second append
        carrying the same ``(session_id, dedup_key)`` is a no-op and returns the
        *existing* seq, so a resume that re-persists its restored window re-links
        its bookkeeping instead of duplicating rows. A ``None`` key is never
        deduped.
        """
        values = self._insert_row(session_id, agent_id, entry, dedup_key)
        columns = ", ".join(_INSERT_COLUMNS)
        placeholders = ", ".join("%s" for _ in _INSERT_COLUMNS)
        conn = get_conn()
        try:
            with dict_cursor(conn) as cur:
                seq = self._alloc_seqs(cur, 1)[0]
                cur.execute(
                    f"INSERT INTO platform.conversation_history "
                    f"(workspace_key, seq, {columns}) "
                    f"VALUES (%s, %s, {placeholders}) "
                    f"ON CONFLICT (workspace_key, session_id, dedup_key) "
                    f"DO NOTHING "
                    f"RETURNING seq",
                    (self._workspace_key, seq, *values),
                )
                row = cur.fetchone()
                if row is None:
                    # Conflict: this event is already durable. Return its seq so
                    # the caller re-links to the existing row. ``ON CONFLICT``
                    # suppresses the insert but not the counter advance, leaving
                    # a gap in ``seq`` — harmless, because ``seq`` is only ever
                    # compared against the ranges recorded in session state, and
                    # the legacy importer preserves the original numbering.
                    cur.execute(
                        "SELECT seq FROM platform.conversation_history "
                        "WHERE workspace_key = %s AND session_id = %s "
                        "AND dedup_key = %s",
                        (self._workspace_key, session_id, dedup_key),
                    )
                    existing = cur.fetchone()
                    conn.commit()
                    return int(existing["seq"]) if existing else 0
            conn.commit()
            return int(row["seq"])
        except PlatformStoreUnavailable:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            put_conn(conn)

    def append_many(
        self,
        *,
        session_id: str,
        entries: Sequence[tuple[LogEntry, str | None]],
        agent_id: str | None = None,
    ) -> int:
        """Append a group of events in one transaction.

        Returns the number of newly inserted rows. Duplicate keys remain no-ops,
        matching :meth:`append`. Intended for backfills, where committing every
        individual row would turn round-trip latency into startup latency.
        """
        if not entries:
            return 0

        columns = ", ".join(_INSERT_COLUMNS)
        placeholders = ", ".join("%s" for _ in _INSERT_COLUMNS)
        sql = (
            f"INSERT INTO platform.conversation_history "
            f"(workspace_key, seq, {columns}) "
            f"VALUES (%s, %s, {placeholders}) "
            f"ON CONFLICT (workspace_key, session_id, dedup_key) DO NOTHING"
        )
        conn = get_conn()
        inserted = 0
        try:
            with dict_cursor(conn) as cur:
                seqs = self._alloc_seqs(cur, len(entries))
                for seq, (entry, dedup_key) in zip(seqs, entries):
                    values = self._insert_row(
                        session_id, agent_id, entry, dedup_key,
                    )
                    cur.execute(sql, (self._workspace_key, seq, *values))
                    inserted += cur.rowcount
            conn.commit()
            return inserted
        except PlatformStoreUnavailable:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            put_conn(conn)

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
        """Refresh an already-appended row in place. ``seq`` is unchanged.

        Used when one logical turn is *extended* after its first write: AgentScope
        accumulates a whole reply into a single assistant Msg, so the durable row
        must end up with every cell's blocks and any later-emitted headline. The
        scalar ``tool_call_id``/``name``/``tool_state``/``tool_input`` and the
        message ``metadata`` are refreshed too, so a turn that grows a *later*
        tool call does not leave them frozen at their first-write values. Omitting
        ``metadata`` keeps the stored value unchanged for older callers.
        """
        # Column names below are hard-coded literals; only values are
        # parameterized. Never add a caller-controlled name here.
        assignments = [
            "content = %s",
            "headline = %s",
            "blocks = %s",
            "tool_call_id = %s",
            "name = %s",
            "tool_state = %s",
            "tool_input = %s",
        ]
        values: list[Any] = [
            strip_nuls(content),
            strip_nuls(headline),
            _to_json(strip_nuls(blocks)),
            strip_nuls(tool_call_id),
            strip_nuls(name),
            strip_nuls(tool_state),
            _to_json(strip_nuls(tool_input)),
        ]
        if metadata is not _UNSET:
            assignments.append("metadata = %s")
            # The schema canonically represents empty metadata as SQL NULL,
            # matching the insert path.
            values.append(_to_json(strip_nuls(metadata) or None))
        # A refreshed row carries the turn's grown tool output, so it needs the
        # same NUL scrubbing as the insert path — PostgreSQL rejects NUL in a
        # text column regardless of which statement supplies it.
        values = strip_nuls(values)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE platform.conversation_history SET "
                    + ", ".join(assignments)
                    + " WHERE workspace_key = %s AND seq = %s",
                    (*values, self._workspace_key, seq),
                )
            conn.commit()
        except PlatformStoreUnavailable:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            put_conn(conn)

    # --- legacy import -------------------------------------------------

    def import_rows(self, rows: Sequence[dict]) -> int:
        """Insert pre-existing rows verbatim, preserving their ``seq``.

        Used only by the SQLite→PostgreSQL upgrade, which must keep the original
        numbering: the eviction index stores ``seq`` ranges inside session state,
        so renumbering imported history would break every existing reference.
        For the same reason the per-workspace counter is raised to the imported
        maximum afterwards — otherwise the next ``append`` would hand out a
        ``seq`` that is already taken and hit the primary key.

        Idempotent: ``ON CONFLICT DO NOTHING`` covers both the ``(workspace_key,
        seq)`` primary key and the ``(workspace_key, session_id, dedup_key)``
        unique index, so re-running an interrupted import inserts only what is
        missing. Returns the number of rows actually inserted.
        """
        if not rows:
            return 0
        columns = ", ".join(_INSERT_COLUMNS)
        placeholders = ", ".join("%s" for _ in _INSERT_COLUMNS)
        sql = (
            f"INSERT INTO platform.conversation_history "
            f"(workspace_key, seq, {columns}) "
            f"VALUES (%s, %s, {placeholders}) "
            f"ON CONFLICT DO NOTHING"
        )
        conn = get_conn()
        inserted = 0
        try:
            with dict_cursor(conn) as cur:
                for row in rows:
                    values = tuple(row.get(column) for column in _INSERT_COLUMNS)
                    cur.execute(
                        sql,
                        (self._workspace_key, int(row["seq"]), *values),
                    )
                    inserted += cur.rowcount
                # Raise the watermark past everything imported so ordinary
                # appends continue after the legacy numbering. ``GREATEST`` keeps
                # this safe to re-run and safe against a later append.
                cur.execute(
                    """
                    INSERT INTO platform.history_seq (workspace_key, last_seq)
                    VALUES (%s, %s)
                    ON CONFLICT (workspace_key)
                    DO UPDATE SET last_seq = GREATEST(
                        platform.history_seq.last_seq,
                        EXCLUDED.last_seq
                    )
                    """,
                    (
                        self._workspace_key,
                        max(int(row["seq"]) for row in rows),
                    ),
                )
            conn.commit()
            return inserted
        except PlatformStoreUnavailable:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            put_conn(conn)

    # --- read path -----------------------------------------------------

    def count(self, session_id: str) -> int:
        conn = get_conn()
        try:
            with dict_cursor(conn) as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM platform.conversation_history "
                    "WHERE workspace_key = %s AND session_id = %s",
                    (self._workspace_key, session_id),
                )
                return int(cur.fetchone()["n"])
        finally:
            put_conn(conn)

    def load_session_messages(
        self,
        session_id: str,
        agent_id: str | None = None,
    ) -> list[Msg]:
        """Rebuild this session's full transcript, oldest first.

        The live context window only holds what Scroll has not evicted, so it is
        *not* the conversation: reading it as the transcript silently dropped
        every evicted turn. These rows are the durable record the model itself
        recalls from, so they are the correct source for a transcript view.

        ``blocks`` holds each block's ``model_dump``; AgentScope coerces those
        dicts back into typed blocks by their ``type`` discriminator, so tool
        calls and media survive rather than degrading to plain text. A row that
        cannot be rebuilt is skipped instead of failing the whole read — a
        partially readable transcript beats none.
        """
        if not session_id:
            return []
        where = ["workspace_key = %s", "session_id = %s"]
        params: list[Any] = [self._workspace_key, session_id]
        if agent_id:
            # Early migration versions wrote agent_id=NULL. Session plus seq
            # range already pins those rows here, so keep them.
            where.append("(agent_id = %s OR agent_id IS NULL)")
            params.append(agent_id)
        conn = get_conn()
        try:
            with dict_cursor(conn) as cur:
                cur.execute(
                    "SELECT seq, kind, role, name, content, blocks, metadata, "
                    "created_at "
                    "FROM platform.conversation_history "
                    "WHERE " + " AND ".join(where) +
                    # Order by the time the turn happened, NOT by ``seq``.
                    #
                    # ``seq`` is the *write* order, and it does not always match
                    # the conversation's order: a turn can be persisted later than
                    # the turn that follows it (a resumed/retried turn, or rows
                    # imported by the session-sync pass), which made the console
                    # render a question *below* its answer after a refresh.
                    #
                    # ``seq`` remains the tie-break: every row of one turn shares
                    # its message's ``created_at``, so the within-turn order — tool
                    # call before its result — is still the write order.
                    #
                    # ``NULLS LAST`` keeps an undated row at the end (keeping its
                    # seq relative to other undated rows) instead of jumping to the
                    # head of the conversation.
                    " ORDER BY created_at ASC NULLS LAST, seq",
                    tuple(params),
                )
                rows = cur.fetchall()
        finally:
            put_conn(conn)

        messages: list[Msg] = []
        seen: set[tuple] = set()
        for row in rows:
            msg = _row_to_msg(row)
            if msg is None:
                continue
            # Collapse rows that describe the same logical event.
            #
            # ``append`` dedupes on ``(session_id, dedup_key)``, so a row is only
            # protected while its key stays identical — and a re-persist under a
            # different key (or a NULL key, which never conflicts) can write the
            # same message twice. Replaying every row then showed a question twice
            # with its answer in between.
            #
            # Identity is (role, name, created_at, text): all rows of one Msg share
            # its ``created_at``, and a user genuinely asking the same thing again
            # gets a new timestamp, so real repetition survives.
            key = (
                row["role"],
                row["name"],
                row.get("created_at"),
                msg.get_text_content() or "",
            )
            if key in seen:
                logger.debug(
                    "scroll: dropped a duplicate history row seq=%s", row["seq"],
                )
                continue
            seen.add(key)
            messages.append(msg)
        return messages

    # --- session export / import ---------------------------------------

    def export_session_rows(
        self,
        session_id: str,
        agent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return this session's transcript as **raw rows**, oldest first.

        Deliberately not ``load_session_messages``: that path rebuilds ``Msg``
        objects, and rebuilding truncates any tool result over
        ``_TRANSCRIPT_TOOL_RESULT_MAX_CHARS`` and recomputes the headline from
        the body. Both are invisible in the console but corrupt a file that is
        meant to restore the conversation elsewhere, so the export works on
        columns instead of messages.

        Every column of ``platform.conversation_history`` is TEXT, so the rows
        are plain JSON-serialisable dicts — no decoding, and therefore nothing
        to lose. ``seq`` is included for readability only; the importer allocates
        fresh sequence numbers (``(workspace_key, seq)`` is the primary key, so
        reusing the original ones would collide with the source session).
        """
        if not session_id:
            return []
        where = ["workspace_key = %s", "session_id = %s"]
        params: list[Any] = [self._workspace_key, session_id]
        if agent_id:
            where.append("(agent_id = %s OR agent_id IS NULL)")
            params.append(agent_id)
        columns = ", ".join(("seq",) + _INSERT_COLUMNS)
        conn = get_conn()
        try:
            with dict_cursor(conn) as cur:
                cur.execute(
                    f"SELECT {columns} FROM platform.conversation_history "
                    "WHERE " + " AND ".join(where) +
                    # Same ordering as the read path: the time the turn happened
                    # first, write order as the tie-break. Kept in step with
                    # ``load_session_messages`` on purpose — an export that
                    # ordered differently would scramble a conversation on
                    # re-import in exactly the way that ordering was added to fix.
                    " ORDER BY created_at ASC NULLS LAST, seq",
                    tuple(params),
                )
                rows = cur.fetchall()
        finally:
            put_conn(conn)
        return [dict(row) for row in rows]

    def import_session_rows(
        self,
        *,
        session_id: str,
        rows: Sequence[dict],
        agent_id: str | None = None,
    ) -> int:
        """Write exported rows into *session_id*, allocating fresh ``seq``s.

        ``import_rows`` (the SQLite→PostgreSQL upgrade path) cannot be used
        here: it preserves the original ``seq``, which is half of the primary
        key and would collide with the rows still living under the source
        session. This goes through ``append_many`` instead, which hands out new
        sequence numbers and dedupes on ``(workspace_key, session_id,
        dedup_key)`` — a new session id never collides with the source, so the
        whole transcript lands.

        Malformed rows are skipped rather than aborting the import: a file is
        user-supplied input, and one unusable row should not cost the user the
        rest of their conversation. Returns the number of rows written.
        """
        entries: list[tuple[LogEntry, str | None]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            entry = _row_to_entry(row)
            if entry is None:
                continue
            dedup_key = row.get("dedup_key")
            entries.append(
                (entry, str(dedup_key) if dedup_key else None),
            )
        if not entries:
            return 0
        return self.append_many(
            session_id=session_id,
            entries=entries,
            agent_id=agent_id,
        )

    def claim_session(self, session_id: str, agent_id: str | None) -> int:
        """Assign legacy unowned rows in a canonical session to an agent."""
        if not session_id or not agent_id:
            return 0
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE platform.conversation_history SET agent_id = %s "
                    "WHERE workspace_key = %s AND session_id = %s "
                    "AND agent_id IS NULL",
                    (agent_id, self._workspace_key, session_id),
                )
                claimed = int(cur.rowcount)
            conn.commit()
            return claimed
        except Exception:
            conn.rollback()
            raise
        finally:
            put_conn(conn)

    def reconcile_session_rows(
        self,
        source_ids: set[str],
        target_id: str,
        dedup_keys: set[str],
        *,
        agent_id: str | None = None,
    ) -> tuple[int, int, int]:
        """Move rows proven to come from one file into its canonical session.

        ``source_ids`` alone is not sufficient provenance because synthetic IDs
        can collide across channel directories. Restricting the operation to
        dedup keys recomputed from the source file prevents unrelated rows from
        being swept into ``target_id``.

        Returns ``(moved, deduplicated, claimed)``. Non-conflicting rows retain
        their original ``seq``; source duplicates are removed in favour of the
        existing canonical row so the unique dedup contract stays valid.
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
        conn = get_conn()
        try:
            with dict_cursor(conn) as cur:
                for source_id in sources:
                    # Chunked: a tool-heavy session can carry thousands of keys
                    # and PostgreSQL has a parameter ceiling per statement.
                    for start in range(0, len(keys), 400):
                        chunk = keys[start : start + 400]
                        rows = self._select_actionable_rows(
                            cur, source_id, chunk, agent_id,
                        )
                        if not rows:
                            continue

                        row_keys = [str(row["dedup_key"]) for row in rows]
                        cur.execute(
                            "SELECT dedup_key FROM platform.conversation_history "
                            "WHERE workspace_key = %s AND session_id = %s "
                            "AND dedup_key = ANY(%s)",
                            (self._workspace_key, target_id, row_keys),
                        )
                        existing_keys = {
                            str(row["dedup_key"]) for row in cur.fetchall()
                        }
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

                        if duplicates:
                            cur.execute(
                                "DELETE FROM platform.conversation_history "
                                "WHERE workspace_key = %s AND seq = ANY(%s)",
                                (
                                    self._workspace_key,
                                    [int(row["seq"]) for row in duplicates],
                                ),
                            )
                            deduplicated += len(duplicates)

                        if movable:
                            cur.execute(
                                "UPDATE platform.conversation_history "
                                "SET session_id = %s, "
                                "agent_id = COALESCE(agent_id, %s) "
                                "WHERE workspace_key = %s AND seq = ANY(%s)",
                                (
                                    target_id,
                                    agent_id,
                                    self._workspace_key,
                                    [int(row["seq"]) for row in movable],
                                ),
                            )
                            moved += int(cur.rowcount)

                if agent_id:
                    cur.execute(
                        "UPDATE platform.conversation_history SET agent_id = %s "
                        "WHERE workspace_key = %s AND session_id = %s "
                        "AND agent_id IS NULL",
                        (agent_id, self._workspace_key, target_id),
                    )
                    claimed = int(cur.rowcount)
            conn.commit()
            return (moved, deduplicated, claimed)
        except Exception:
            conn.rollback()
            raise
        finally:
            put_conn(conn)

    def _select_actionable_rows(
        self,
        cur,
        source_id: str,
        chunk: list[str],
        agent_id: str | None,
    ) -> list[dict]:
        """Rows in *source_id* whose dedup keys prove they belong to this file."""
        ownership = ""
        params: list[Any] = [self._workspace_key, source_id, chunk]
        if agent_id:
            ownership = " AND (agent_id = %s OR agent_id IS NULL)"
            params.append(agent_id)
        cur.execute(
            "SELECT seq, dedup_key, content FROM platform.conversation_history "
            "WHERE workspace_key = %s AND session_id = %s "
            "AND dedup_key = ANY(%s)" + ownership,
            params,
        )
        return list(cur.fetchall())

    def existing_seqs(self, seqs: set[int]) -> set[int]:
        """Return the subset of this workspace's ``seq`` values that exist."""
        if not seqs:
            return set()
        ordered = sorted(int(seq) for seq in seqs)
        conn = get_conn()
        try:
            with dict_cursor(conn) as cur:
                cur.execute(
                    "SELECT seq FROM platform.conversation_history "
                    "WHERE workspace_key = %s AND seq = ANY(%s)",
                    (self._workspace_key, ordered),
                )
                return {int(row["seq"]) for row in cur.fetchall()}
        finally:
            put_conn(conn)

    def contents_by_seqs(self, seqs: set[int]) -> dict[int, str | None]:
        """Return exact persisted content for the given ``seq`` values.

        Summary evidence uses this after live tool results have been folded.
        Querying exact keys, instead of a broad ``lo..hi`` range, prevents
        interleaved rows from another agent or session entering the evidence.
        """
        if not seqs:
            return {}
        ordered = sorted(int(seq) for seq in seqs)
        found: dict[int, str | None] = {}
        conn = get_conn()
        try:
            with dict_cursor(conn) as cur:
                # Chunked so a tool-heavy summary does not build one enormous
                # array parameter.
                for start in range(0, len(ordered), 500):
                    chunk = ordered[start : start + 500]
                    cur.execute(
                        "SELECT seq, content FROM platform.conversation_history "
                        "WHERE workspace_key = %s AND seq = ANY(%s)",
                        (self._workspace_key, chunk),
                    )
                    found.update(
                        {int(row["seq"]): row["content"] for row in cur.fetchall()},
                    )
        finally:
            put_conn(conn)
        return found

    @staticmethod
    def _purge_where(
        before: str,
        kinds: tuple[str, ...] | None,
    ) -> tuple[str, list]:
        """Build the shared ``WHERE`` for purge/estimate so they cannot drift.

        Always bounds by ``created_at < before`` (a NULL ``created_at`` is never
        matched, so unstamped rows are retained). When ``kinds`` is given, also
        restricts to those row kinds (e.g. ``("tool_result",)`` to drop only tool
        output and keep the conversation). Values are bound, never interpolated;
        the workspace predicate is prepended by the callers.
        """
        clause = "created_at IS NOT NULL AND created_at < %s"
        params: list = [before]
        if kinds:
            clause += " AND kind = ANY(%s)"
            params.append(list(kinds))
        return clause, params

    def estimate_purge(
        self,
        *,
        before: str,
        kinds: tuple[str, ...] | None = None,
    ) -> dict:
        """How much ``purge(before=...)`` would remove — WITHOUT removing it.

        Returns ``{"rows": n, "content_bytes": b}`` where ``content_bytes`` is the
        summed length of the ``content`` column for the matched rows (the bulk of
        the on-disk weight). ``kinds`` narrows to specific row kinds. A dry-run
        estimate to show an operator before they commit a purge.
        """
        where, params = self._purge_where(before, kinds)
        conn = get_conn()
        try:
            with dict_cursor(conn) as cur:
                cur.execute(
                    "SELECT COUNT(*) AS rows, "
                    "COALESCE(SUM(LENGTH(content)), 0) AS content_bytes "
                    "FROM platform.conversation_history "
                    "WHERE workspace_key = %s AND " + where,
                    (self._workspace_key, *params),
                )
                row = cur.fetchone()
            return {
                "rows": int(row["rows"]),
                "content_bytes": int(row["content_bytes"]),
            }
        finally:
            put_conn(conn)

    def purge(
        self,
        *,
        before: str,
        dry_run: bool = False,
        kinds: tuple[str, ...] | None = None,
    ) -> int:
        """Delete this workspace's history rows with ``created_at < before``.

        Returns the number of rows that match (and, unless ``dry_run``, were
        removed). ``kinds`` narrows the delete to specific row kinds — e.g.
        ``("tool_result",)`` drops only tool output (the bulk of the weight) while
        keeping the conversation turns. Rows with a NULL/empty ``created_at`` are
        never matched, so they are retained. This is the retention/clear path,
        driven on startup and teardown by ``history_retention_days`` (default 30;
        set 0 to keep history forever, which never calls this).
        """
        where, params = self._purge_where(before, kinds)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                if dry_run:
                    cur.execute(
                        "SELECT COUNT(*) FROM platform.conversation_history "
                        "WHERE workspace_key = %s AND " + where,
                        (self._workspace_key, *params),
                    )
                    matched = int(cur.fetchone()[0])
                else:
                    cur.execute(
                        "DELETE FROM platform.conversation_history "
                        "WHERE workspace_key = %s AND " + where,
                        (self._workspace_key, *params),
                    )
                    matched = int(cur.rowcount)
            conn.commit()
            return matched
        except Exception:
            conn.rollback()
            raise
        finally:
            put_conn(conn)

    def vacuum(self) -> None:
        """Reclaim space freed by :meth:`purge`.

        SQLite needed an explicit ``VACUUM`` to shrink the file. PostgreSQL's
        autovacuum reclaims dead tuples in the background, so there is nothing to
        do here; kept as a no-op because it is part of the retention path's call
        sequence and callers should not have to special-case the backend.
        """
        return

    def note_write_failure(self, exc: BaseException) -> None:
        """Record a write-through failure — durability is now degraded.

        Logs prominently on the first failure, then counts the rest to avoid log
        spam. Read ``degraded`` to gate any "fully durable" guarantee.
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
        """True once :meth:`close` has run."""
        return self._closed

    def close(self) -> None:
        """Mark this store closed.

        Connections are pooled and returned per call, so there is nothing to
        release; the flag exists so callers can distinguish an intentional
        teardown from a real outage.
        """
        self._closed = True

    def __repr__(self) -> str:
        return f"<HistoryStore workspace={self._workspace_key}>"


def _to_json(value) -> str | None:
    """Encode a structured column, removing NULs before they get escaped.

    ``json.dumps`` renders a NUL as the ASCII escape ``\\u0000``, which survives
    a string-level strip and comes back as a real NUL on load. Cleaning here —
    the last step before encoding — keeps callers from having to remember the
    ordering.
    """
    if value is None:
        return None
    value = strip_nuls(value)
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


# PostgreSQL text columns cannot store NUL (0x00): ``psycopg2`` refuses the
# value client-side with ``ValueError: A string literal cannot contain NUL
# (0x00) characters.`` before the server ever sees it. Forensics tool output
# routinely carries embedded NULs, so every written row is scrubbed — see
# ``qwenpaw.utils.text_safety`` for why they are stripped rather than escaped.



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


# Transcript payload budget for one tool result.
#
# Returning the whole durable transcript (instead of only the live window) is
# required for navigation, but tool outputs are unbounded: it is normal for one
# result to hold tens of KB, so 500 such turns produced a 19MB response and a
# 1000-turn session tens of MB — which the console then had to download and
# parse. The transcript view only needs enough to recognise the call; the full
# text stays in the store and is recoverable through the recall tools.
_TRANSCRIPT_TOOL_RESULT_MAX_CHARS = 4000
_TRANSCRIPT_TRUNCATED_MARK = "\n\n…[内容过长已截断，完整内容可用 recall 工具获取]"


def _truncate_tool_result(block: Any) -> Any:
    """Cap one tool result's rendered output for the transcript view.

    ``output`` is either a string or a list of blocks; both shapes are bounded so
    no single result can dominate the response.
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


_BLOCK_TYPES: dict[str, Any] | None = None


def _coerce_block(item: Any) -> Any:
    """Turn one stored block dict into its typed AgentScope block.

    ``Msg.content`` is a union whose **first** member is ``TextBlock``, and
    pydantic does not fall through to the other members once a member's literal
    ``type`` fails — so handing ``Msg`` raw dicts rejects every non-text block.
    Resolving the concrete class by ``type`` first keeps tool calls, tool results,
    thinking and media intact instead of degrading a turn to text.

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


def _row_to_entry(row: Any) -> LogEntry | None:
    """Turn one exported row back into a ``LogEntry`` for re-insertion.

    The structured columns (``tool_input`` / ``blocks`` / ``metadata``) are
    copied across as the **raw text** they were read as, not decoded and
    re-encoded. ``_to_json`` passes a ``str`` through untouched, so the value
    written back is byte-identical to the value that came out — which is the
    point of the exercise. Decoding first would not round-trip: a ``tool_input``
    that was a bare string is stored unquoted (``git status``), and ``_loads``
    returns ``None`` for that because it is not valid JSON, so the argument
    would silently vanish on import.

    Returns ``None`` for a row that cannot be a durable entry (missing or
    unknown ``kind``), so the caller can skip it instead of writing a row the
    schema will reject.
    """
    if not isinstance(row, dict):
        return None
    kind = row.get("kind")
    if kind not in ("model_turn", "context_msg", "tool_result"):
        return None
    return LogEntry(
        kind=kind,
        role=row.get("role"),
        name=row.get("name"),
        content=row.get("content"),
        metadata=row.get("metadata"),
        tool_call_id=row.get("tool_call_id"),
        tool_input=row.get("tool_input"),
        tool_state=row.get("tool_state"),
        headline=row.get("headline"),
        blocks=row.get("blocks"),
        created_at=row.get("created_at"),
    )


def _row_to_msg(row: Any) -> Msg | None:
    """Rebuild one durable row into a ``Msg`` for the transcript view.

    ``blocks`` is the faithful copy of the turn's content (each block's
    ``model_dump``), so it wins over the flattened ``content`` text, which has
    already lost tool calls and media. A row with no usable content — or one that
    fails to parse at all — is dropped rather than raising, so a single bad row
    cannot blank an entire conversation.
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
    # ``sessionApi.parseTimestamp``), and ``Msg.timestamp`` aliases
    # ``created_at``. Without restoring it here, every rebuilt message had no time
    # at all, so the frontend could not order a turn's request against its reply —
    # after a refresh the question rendered *below* the answer.
    created_at = row.get("created_at")
    if created_at and "timestamp" not in metadata:
        metadata["timestamp"] = created_at
    try:
        # ``Msg.created_at`` rejects None, so it is passed only when present.
        fields: dict[str, Any] = {
            "name": row["name"] or role,
            "role": role,
            "content": content,
            "metadata": metadata,
            # A stable id per row. ``Msg`` would otherwise mint a fresh random
            # uuid on every read, so the same message looked like a different one
            # each time the transcript was fetched (and the console keys its cards
            # by id).
            "id": f"hist-{row['seq']}",
        }
        if created_at:
            fields["created_at"] = created_at
        return Msg(**fields)
    except Exception:  # noqa: BLE001 - one bad row must not break the read
        logger.debug(
            "scroll: could not rebuild history row seq=%s", row.get("seq"),
            exc_info=True,
        )
        return None


__all__ = ["HistoryStore", "RECALL_TOOL_NAMES", "workspace_key_for"]
