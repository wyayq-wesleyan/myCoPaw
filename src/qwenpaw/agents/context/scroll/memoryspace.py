# -*- coding: utf-8 -*-
"""The model's read-only recall surface over durable conversation history.

Durable history lives in the platform's PostgreSQL store
(``platform.conversation_history``). Every operation here is a bound,
parameterized query scoped to this workspace's ``workspace_key``, so one account
can never read another's rows.

**Why this is no longer SQLite.** It used to be an in-memory SQLite scratch
database with the workspace's ``history.db`` ATTACHed read-only, and an
authorizer blocking writes to ``hist``. Isolation came from the *file*: each
workspace had its own database, so arbitrary model-issued SQL could only ever
reach that workspace's rows. PostgreSQL is shared by every tenant, so that
guarantee disappears — a model-authored ``SELECT * FROM
platform.conversation_history`` would return every account's conversations.
There is no per-connection authorizer in PostgreSQL, so the fix is structural
rather than defensive: this class exposes only the parameterized structured
operations below, and the escape hatch that ran arbitrary SQL (the sandboxed
``recall_history_python`` REPL) is no longer offered. ``workspace_key`` is a
bound parameter on *every* statement, which is why it appears first in each
``where`` list below.

Returned rows are capped (``row_cap``) so a runaway query can't flood the
model's context; truncation is flagged with a trailing ``_truncated`` row.
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2.errors

from ....platform_store import (
    PlatformStoreUnavailable,
    dict_cursor,
    get_conn,
    put_conn,
)

# Raised when ``statement_timeout`` cancels a query; handled as a search
# deadline in :meth:`MemorySpace.search`.
QueryCanceled = psycopg2.errors.QueryCanceled

_DEFAULT_ROW_CAP = 1000
_DEFAULT_SEARCH_TURN_MAX_BYTES = 4 * 1024 * 1024
_DEFAULT_SEARCH_MAX_SECONDS = 5.0
_SEARCH_HIT_PAGE_SIZE = 64
_TURN_BOUNDARY_BATCH_SIZE = 200
_SAVED_TOOL_CANDIDATE_PAGE_SIZE = 200
_SAVED_TOOL_SCAN_MAX_BYTES = 32 * 1024 * 1024
_SAVED_TOOL_SCAN_MAX_SECONDS = 2.0

# Ceiling for a single statement's ``statement_timeout``. Every recall query is
# bounded by the caller's search deadline, but a long first-time index scan must
# still not hang a pool slot indefinitely.
_MAX_STATEMENT_MS = 60_000

_HISTORY_TABLE = "platform.conversation_history"

# The recall tool's own turns (its output) are durable but must never surface as
# *search hits* — otherwise a query matches the agent's own earlier queries and
# tracebacks (self-pollution). Rows are filtered out here at read time.
# ``recall_history_python`` is the removed REPL: its name is kept so rows written
# before the PostgreSQL migration stay excluded instead of resurfacing as hits.
_RECALL_TOOL_NAMES = ("recall_history", "recall_history_python")
_RECALL_EXCL_PLACEHOLDERS = ", ".join("%s" for _ in _RECALL_TOOL_NAMES)

# User-role stub rows the runtime injects to keep a turn going ("Continue
# working on the task."). They are not real requests: the active-turn floor
# must anchor on the request that STARTED the turn, or the floor jumps to the
# stub and the real request becomes searchable mid-turn again (echo loop).
# Values must match SYNTHETIC_USER_MESSAGE_TAGS in qwenpaw.constant (this
# module avoids importing the package for that constant alone).
_SYNTHETIC_USER_TAGS = (
    "loop_continuation",
    "auto_continue",
    "rubric_evaluation",
)

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T"
    r"\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:\d{2})?$",
)
_SAVED_TOOL_FILE_RE = re.compile(
    r"call `read_file` with file_path="
    r'(?:"(?P<quoted>[^"]*)"|(?P<legacy>.+?))'
    r"\s+start_line=(?P<start_line>\d+)",
)

_WORD_RE = re.compile(r"\w+", re.UNICODE)
# Bare uppercase boolean operators are honoured so a query like
# ``tank OR aquarium`` casts a wide net; every other token is a literal term.
# A lowercase ``or`` stays a search term.
_SEARCH_OPERATORS = frozenset({"AND", "OR", "NOT"})


@dataclass
class _ScanBudget:
    """Shared byte budget for one saved-artifact recall operation."""

    remaining: int
    deadline: float
    exhausted: bool = False

    def is_exhausted(self) -> bool:
        if not self.exhausted and time.monotonic() >= self.deadline:
            self.exhausted = True
        return self.exhausted

    def read_line(self, file_obj) -> bytes | None:  # noqa: ANN001
        """Read at most the remaining budget from one binary line."""
        if self.remaining <= 0 or self.is_exhausted():
            self.exhausted = True
            return None
        raw = file_obj.readline(self.remaining + 1)
        if not raw:
            return b""
        if len(raw) > self.remaining:
            raw = raw[: self.remaining]
            self.remaining = 0
            self.exhausted = True
            return raw
        self.remaining -= len(raw)
        return raw


@dataclass
class _SearchBudget:
    """Total resource budget for one history search and turn expansion."""

    remaining_rows: int
    remaining_bytes: int
    deadline: float
    max_rows: int
    max_bytes: int
    exhausted_reason: str | None = None
    timed_out: bool = False

    def ensure_time(self) -> None:
        if self.timed_out or time.monotonic() >= self.deadline:
            self.timed_out = True
            raise TimeoutError("history search exceeded its execution timeout")

    def remaining_ms(self) -> int:
        """Milliseconds left before the deadline, clamped to a valid timeout.

        ``statement_timeout`` is a 32-bit integer, so an unclamped value from a
        distant deadline would be rejected outright rather than applied.
        """
        return max(1, min(int((self.deadline - time.monotonic()) * 1000), _MAX_STATEMENT_MS))

    def has_turn_capacity(self) -> bool:
        self.ensure_time()
        if self.remaining_rows <= 0:
            self.exhausted_reason = "total turn row budget"
            return False
        if self.remaining_bytes <= 0:
            self.exhausted_reason = "total turn byte budget"
            return False
        return True

    def consume_turn_row(self, row: dict) -> bool:
        if not self.has_turn_capacity():
            return False
        encoded_size = len(
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8"),
        )
        if encoded_size > self.remaining_bytes:
            self.exhausted_reason = "total turn byte budget"
            return False
        self.remaining_rows -= 1
        self.remaining_bytes -= encoded_size
        return True


def _fetch(
    sql: str,
    params: tuple | list = (),
    *,
    timeout_ms: int | None = None,
) -> list[dict]:
    """Run one read-only statement and return dict-shaped rows.

    ``timeout_ms`` is applied with ``SET LOCAL`` inside an explicit
    transaction, so it reverts when the transaction ends and cannot leak a
    shortened timeout onto the next borrower of this pooled connection.
    """
    conn = get_conn()
    try:
        with dict_cursor(conn) as cur:
            if timeout_ms is not None:
                cur.execute(
                    "SET LOCAL statement_timeout = %s",
                    (int(timeout_ms),),
                )
            cur.execute(sql, params or ())
            rows = [dict(row) for row in cur.fetchall()]
        conn.commit()
        return rows
    except Exception:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001 - preserve the original failure
            pass
        raise
    finally:
        put_conn(conn)


def _or_query_groups(raw: str) -> list[str]:
    """Split a valid bare-uppercase ``OR`` query into alternative groups.

    Whitespace-separated terms inside each group retain implicit-AND
    semantics. Malformed leading, trailing, or repeated ``OR`` is kept as one
    literal group so the fallback remains restrictive instead of broadening a
    bad query by discarding an empty alternative.
    """
    tokens = raw.split()
    if not tokens:
        return [raw]
    groups: list[str] = []
    current: list[str] = []
    for token in tokens:
        if token == "OR":
            if not current:
                return [raw]
            groups.append(" ".join(current))
            current = []
        else:
            current.append(token)
    if not current:
        return [raw]
    groups.append(" ".join(current))
    return groups


def _like_search_terms(raw: str) -> list[str]:
    """Whitespace-delimited literal terms for the search path."""
    terms = raw.split()
    # Keep direct/internal all-whitespace calls restrictive instead of
    # accidentally producing a predicate-free query that returns every row.
    return terms or [raw]


def _like_search_groups(raw: str) -> list[list[str]]:
    """Literal terms grouped as implicit AND arms joined by OR."""
    return [_like_search_terms(group) for group in _or_query_groups(raw)]


def _like_pattern(term: str) -> str:
    """Wrap one literal term as an SQL contains-pattern."""
    escaped = (
        term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


def parse_date(value: object) -> date:
    """Strictly parse an ISO date or timestamp into its calendar date.

    Accepted strings are ``YYYY-MM-DD`` and ISO timestamps using the ``T``
    separator, optionally with seconds, fractional seconds, and ``Z`` or a
    ``+/-HH:MM`` offset. A timestamp keeps the calendar date written in its
    own stated timezone; it is not converted to the host timezone or UTC.
    Native :class:`date` and :class:`datetime` values are also accepted.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise TypeError(
            "date value must be an ISO string, date, or datetime; "
            f"got {type(value).__name__}",
        )
    try:
        if _ISO_DATE_RE.fullmatch(value):
            return date.fromisoformat(value)
        if _ISO_TIMESTAMP_RE.fullmatch(value):
            return datetime.fromisoformat(value).date()
    except ValueError as exc:
        raise ValueError(
            f"invalid ISO date or timestamp {value!r}: {exc}",
        ) from exc
    raise ValueError(
        f"invalid ISO date or timestamp {value!r}; expected YYYY-MM-DD or "
        "YYYY-MM-DDTHH:MM[:SS[.fraction]][Z|+/-HH:MM]",
    )


class MemorySpace:
    """Read-only, workspace-scoped recall over durable history.

    Every query is filtered by ``workspace_key``; the model supplies only bound
    parameters, never SQL. Returned rows are capped (``row_cap``) so a runaway
    query can't flood the model's context; truncation is flagged with a trailing
    ``_truncated`` row.
    """

    def __init__(
        self,
        *,
        history_db_path: str | Path | None = None,
        workspace_key: str | None = None,
        session_id: str | None = None,
        agent_id: str | None = None,
        row_cap: int = _DEFAULT_ROW_CAP,
        saved_tool_scan_max_bytes: int = _SAVED_TOOL_SCAN_MAX_BYTES,
        saved_tool_scan_max_seconds: float = _SAVED_TOOL_SCAN_MAX_SECONDS,
        search_turn_max_rows: int = _DEFAULT_ROW_CAP,
        search_turn_max_bytes: int = _DEFAULT_SEARCH_TURN_MAX_BYTES,
        search_max_seconds: float = _DEFAULT_SEARCH_MAX_SECONDS,
    ) -> None:
        # The workspace key is the tenant boundary: without it a query would
        # span every account, so refuse rather than guess. Callers normally
        # pass ``history_db_path`` (the workspace's legacy handle) and the key
        # is derived from it exactly as ``HistoryStore`` derives it, so both
        # sides agree on which rows belong to this workspace.
        if workspace_key is None:
            if history_db_path is None:
                raise ValueError(
                    "MemorySpace needs workspace_key or history_db_path to "
                    "scope history to one workspace",
                )
            from .history import workspace_key_for

            workspace_key = workspace_key_for(history_db_path)
        self._workspace_key = workspace_key
        self._row_cap = row_cap
        self._session_id = session_id
        self._agent_id = agent_id
        self._saved_tool_scan_max_bytes = max(0, saved_tool_scan_max_bytes)
        self._saved_tool_scan_max_seconds = max(
            0.0,
            saved_tool_scan_max_seconds,
        )
        self._search_turn_max_rows = max(0, int(search_turn_max_rows))
        self._search_turn_max_bytes = max(0, int(search_turn_max_bytes))
        self._search_max_seconds = max(0.0, float(search_max_seconds))
        # Saved tool artifacts live under the workspace dir alongside the
        # history handle; that root bounds which files a hit may point at.
        self._history_root = (
            Path(history_db_path).expanduser().resolve().parent
            if history_db_path is not None
            else None
        )
        # Cached active-turn floor: the durable store is append-only in
        # practice, so MAX(seq) is computed at most once per instance.
        # A separate flag distinguishes "not computed yet" from a real
        # ``None`` result (no session / no active user turn).
        self._floor_cache: int | None = None
        self._floor_computed: bool = False

    @property
    def workspace_key(self) -> str:
        """The tenant boundary every query is filtered by."""
        return self._workspace_key

    @property
    def session_id(self) -> str | None:
        """The current session id (this conversation)."""
        return self._session_id

    @property
    def agent_id(self) -> str | None:
        """The current agent id — scopes recall to this agent across
        sessions."""
        return self._agent_id

    def _query(
        self,
        sql: str,
        params: tuple | list = (),
        *,
        deadline: float | None = None,
    ) -> list[dict]:
        """Execute a read query and return capped, dict-shaped rows."""
        timeout_ms: int | None = None
        if deadline is not None:
            remaining = max(0.0, deadline - time.monotonic())
            timeout_ms = max(1, min(int(remaining * 1000), _MAX_STATEMENT_MS))
        rows = _fetch(sql, params, timeout_ms=timeout_ms)
        if len(rows) > self._row_cap:
            return rows[: self._row_cap] + [
                {"_truncated": True, "_row_cap": self._row_cap},
            ]
        return rows

    # -- intent-named recall over durable history -----------------------------

    def expand(self, lo: int, hi: int) -> list[dict]:
        """Full durable turns in the seq span ``[lo, hi]``, oldest first.

        ``seq`` is unique per workspace, but rows from concurrent
        conversations can interleave inside a range. The eviction index belongs
        to this MemorySpace's conversation, so retain its session and agent
        lineage.
        """
        where = ["workspace_key = %s", "seq BETWEEN %s AND %s"]
        params: list = [self._workspace_key, int(lo), int(hi)]
        if self._session_id:
            where.append("session_id = %s")
            params.append(self._session_id)
        if self._agent_id:
            # Early migration versions wrote agent_id=NULL. Session + seq range
            # already pins these rows to this eviction lineage, so retain them
            # until startup reconciliation can claim the canonical session.
            where.append("(agent_id = %s OR agent_id IS NULL)")
            params.append(self._agent_id)
        return self._query(
            "SELECT seq, kind, role, name, content, headline, blocks, "
            "metadata, created_at "
            f"FROM {_HISTORY_TABLE} "
            "WHERE " + " AND ".join(where) + " ORDER BY seq",
            tuple(params),
        )

    def recall_tool(
        self,
        tool_call_id: str,
        *,
        all_agents: bool = False,
    ) -> list[dict]:
        """Re-read a tool call and its result by ``tool_call_id``.

        Scoped to this agent's history by default — tool-call ids are not
        globally unique, so widening risks cross-agent collisions; pass
        ``all_agents=True`` only when you mean to. Returns the matching rows
        oldest-first (typically the call turn followed by its result).
        """
        where = ["workspace_key = %s", "tool_call_id = %s"]
        params: list = [self._workspace_key, str(tool_call_id)]
        if not all_agents and self._agent_id:
            where.append("agent_id = %s")
            params.append(self._agent_id)
        rows = self._query(
            "SELECT seq, kind, role, name, tool_input, tool_state, content, "
            "blocks, metadata, created_at "
            f"FROM {_HISTORY_TABLE} "
            "WHERE " + " AND ".join(where) + " ORDER BY seq",
            tuple(params),
        )
        return self._attach_saved_tool_file_matches(rows, "")

    def sessions(
        self,
        *,
        all_agents: bool = False,
        limit: int = 50,
    ) -> list[dict]:
        """List the conversations recorded in durable history.

        Scoped to this agent's own sessions by default (e.g. the live chat
        plus any ``cron:<job-id>`` / ``main`` heartbeat sessions it has run);
        pass ``all_agents=True`` for every agent in the workspace. Each row is
        a ``session_id`` with its turn count and ``seq``/time span — use it to
        discover a session, then read it with :meth:`session`.
        """
        where: list[str] = ["workspace_key = %s"]
        params: list = [self._workspace_key]
        if not all_agents and self._agent_id:
            where.append("agent_id = %s")
            params.append(self._agent_id)
        params.append(int(limit))
        return self._query(
            "SELECT session_id, COUNT(*) AS turns, MIN(seq) AS first_seq, "
            "MAX(seq) AS last_seq, MAX(created_at) AS last_at "
            f"FROM {_HISTORY_TABLE} "
            f"WHERE {' AND '.join(where)} "
            "GROUP BY session_id ORDER BY last_seq DESC LIMIT %s",
            tuple(params),
        )

    def session(
        self,
        session_id: str,
        *,
        all_agents: bool = False,
        limit: int = 200,
    ) -> list[dict]:
        """Read one conversation's turns oldest-first, by ``session_id``.

        The companion to :meth:`sessions` — e.g.
        ``ms.session("cron:nightly-report")`` reconstructs exactly what that
        scheduled job said and did. Scoped to this agent's history by default:
        session ids are not globally unique (``main``, ``local``,
        ``cron:<job>`` recur across agents in a shared workspace), so widening
        risks reading another agent's conversation. Pass ``all_agents=True``
        only when you mean to span every agent.
        """
        where = ["workspace_key = %s", "session_id = %s"]
        params: list = [self._workspace_key, str(session_id)]
        if not all_agents and self._agent_id:
            where.append("agent_id = %s")
            params.append(self._agent_id)
        params.append(int(limit))
        return self._query(
            "SELECT seq, kind, role, name, headline, content, created_at "
            f"FROM {_HISTORY_TABLE} "
            "WHERE " + " AND ".join(where) + " ORDER BY seq LIMIT %s",
            tuple(params),
        )

    def agents(self, *, limit: int = 50) -> list[dict]:
        """List every agent that has written history in this workspace.

        Always workspace-wide (a discovery/ops view), so it can surface other
        agents — each row is an ``agent_id`` with its session and turn counts.
        """
        return self._query(
            "SELECT agent_id, COUNT(DISTINCT session_id) AS sessions, "
            "COUNT(*) AS turns, MAX(created_at) AS last_at "
            f"FROM {_HISTORY_TABLE} "
            "WHERE workspace_key = %s "
            "GROUP BY agent_id ORDER BY last_at DESC LIMIT %s",
            (self._workspace_key, int(limit)),
        )

    def _scope_filters(
        self,
        all_agents: bool,
        session_id: str | None,
        agent_id: str | None,
    ) -> list[tuple[str, str]]:
        """Resolve the ``(column, value)`` lineage filters for a search.

        An explicit ``session_id`` and/or ``agent_id`` pin the search to that
        conversation / agent (AND-combined). With neither given, the default
        is this agent's own cross-session history; ``all_agents`` drops the
        filter to span every agent in the workspace.
        """
        if session_id is not None or agent_id is not None:
            pinned: list[tuple[str, str]] = []
            if session_id is not None:
                pinned.append(("session_id", session_id))
            if agent_id is not None:
                pinned.append(("agent_id", agent_id))
            return pinned
        if all_agents:
            return []
        if self._agent_id:
            return [("agent_id", self._agent_id)]
        return []

    def _active_turn_floor(self) -> int | None:
        """Seq of the current session's latest real user message, or None.

        Everything at or after it is the ACTIVE TURN — the request the agent
        is answering right now plus its in-progress reply, all still in the
        live window (folded tool results carry their own expand address).
        ``search`` excludes that span: without it, a second recall round
        top-matches the agent's OWN in-progress turn — the previous round's
        quoted findings and the request itself — and the echoes drown the real
        hits. ``expand`` / ``recall_tool`` / ``session`` stay unfiltered
        (verbatim replay is their point).

        Memoized per instance: a single ``search`` can consult the floor more
        than once, and the underlying history is append-only, so the MAX(seq)
        scan runs at most once per MemorySpace rather than once per call site.
        """
        if not self._floor_computed:
            self._floor_cache = self._compute_active_turn_floor()
            self._floor_computed = True
        return self._floor_cache

    def _compute_active_turn_floor(self) -> int | None:
        """Uncached MAX(seq) scan behind :meth:`_active_turn_floor`.

        Anchors on the latest REAL user request: runtime continuation stubs
        (tagged user-role rows) extend a turn rather than starting one, so
        they never move the floor.
        """
        if not self._session_id:
            return None
        where = [
            "workspace_key = %s",
            "session_id = %s",
            "kind = 'context_msg'",
            "role = 'user'",
        ]
        params: list = [self._workspace_key, self._session_id]
        if self._agent_id:
            where.append("agent_id = %s")
            params.append(self._agent_id)
        for tag in _SYNTHETIC_USER_TAGS:
            where.append("(metadata IS NULL OR metadata NOT LIKE %s)")
            params.append(f'%"{tag}"%')
        try:
            rows = _fetch(
                f"SELECT MAX(seq) AS s FROM {_HISTORY_TABLE} "
                "WHERE " + " AND ".join(where),
                tuple(params),
                timeout_ms=_MAX_STATEMENT_MS,
            )
        except PlatformStoreUnavailable:
            return None
        value = rows[0]["s"] if rows else None
        return int(value) if value is not None else None

    def _active_turn_exclusion(
        self,
        prefix: str = "",
    ) -> tuple[str, list] | None:
        """``(clause, params)`` excluding the active turn from a search."""
        floor = self._active_turn_floor()
        if floor is None:
            return None
        conds = [f"{prefix}session_id = %s"]
        params: list = [self._session_id]
        if self._agent_id:
            conds.append(f"{prefix}agent_id = %s")
            params.append(self._agent_id)
        conds.append(f"{prefix}seq >= %s")
        params.append(floor)
        return "NOT (" + " AND ".join(conds) + ")", params

    @staticmethod
    def _created_bounds(
        *,
        created_on: str | None,
        created_from: str | None,
        created_to: str | None,
    ) -> tuple[str | None, str | None]:
        """Normalize inclusive calendar-date filters to a half-open span."""
        if created_on is not None and (
            created_from is not None or created_to is not None
        ):
            raise ValueError(
                "created_on cannot be combined with created_from/created_to",
            )
        if created_on is not None:
            day = parse_date(created_on)
            return day.isoformat(), MemorySpace._date_upper_bound(day)
        lower = parse_date(created_from) if created_from is not None else None
        final = parse_date(created_to) if created_to is not None else None
        if lower is not None and final is not None and lower > final:
            raise ValueError("created_from must not be after created_to")
        return (
            lower.isoformat() if lower is not None else None,
            (
                MemorySpace._date_upper_bound(final)
                if final is not None
                else None
            ),
        )

    @staticmethod
    def _date_upper_bound(day: date) -> str:
        """Return a lexicographic exclusive upper bound for one ISO day.

        ``date.max + timedelta(days=1)`` overflows. ``9999-12-32`` is not a
        parseable date, but it is a safe SQL string sentinel: every valid ISO
        timestamp on ``9999-12-31`` sorts before it, and there is no later
        representable calendar date.
        """
        if day == date.max:
            return "9999-12-32"
        return (day + timedelta(days=1)).isoformat()

    @staticmethod
    def _created_conditions(
        bounds: tuple[str | None, str | None],
        *,
        prefix: str = "",
    ) -> tuple[list[str], list[str]]:
        """SQL predicates for an indexed half-open ``created_at`` span."""
        lower, upper = bounds
        conditions: list[str] = []
        params: list[str] = []
        if lower is not None:
            conditions.append(f"{prefix}created_at >= %s")
            params.append(lower)
        if upper is not None:
            conditions.append(f"{prefix}created_at < %s")
            params.append(upper)
        return conditions, params

    def search(
        self,
        query: str,
        *,
        session_id: str | None = None,
        agent_id: str | None = None,
        all_agents: bool = False,
        kind: str | None = None,
        k: int = 10,
        include_turn: bool = True,
        created_on: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
    ) -> list[dict]:
        """Run one bounded history search.

        Statements are cancelled server-side once ``search_max_seconds``
        elapses. Complete turn expansion additionally shares one total row and
        serialized-byte budget across all distinct result turns.
        """
        budget = _SearchBudget(
            remaining_rows=self._search_turn_max_rows,
            remaining_bytes=self._search_turn_max_bytes,
            deadline=time.monotonic() + self._search_max_seconds,
            max_rows=self._search_turn_max_rows,
            max_bytes=self._search_turn_max_bytes,
        )
        budget.ensure_time()
        try:
            return self._search_impl(
                query,
                session_id=session_id,
                agent_id=agent_id,
                all_agents=all_agents,
                kind=kind,
                k=k,
                include_turn=include_turn,
                created_on=created_on,
                created_from=created_from,
                created_to=created_to,
                _budget=budget,
            )
        except (PlatformStoreUnavailable, QueryCanceled) as exc:
            if budget.timed_out or time.monotonic() >= budget.deadline:
                raise TimeoutError(
                    "history search exceeded its execution timeout",
                ) from exc
            raise

    def _search_impl(
        self,
        query: str,
        *,
        session_id: str | None = None,
        agent_id: str | None = None,
        all_agents: bool = False,
        kind: str | None = None,
        k: int = 10,
        include_turn: bool = True,
        created_on: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        _budget: _SearchBudget,
    ) -> list[dict]:
        """Search ``content`` for the query's literal terms.

        Returns up to ``k`` matches, most recent first. By default each match
        keeps its row-shaped compatibility fields (``seq``, ``role``,
        ``content``, ...) and also carries the complete containing turn in
        ``turn``. The turn starts at the nearest real user row in the same
        ``session_id``/``agent_id`` lineage and ends before the next real user
        row. Matches from the same turn are deduplicated and their seqs are
        collected in ``matched_seqs``. ``turn_end_seq`` is the actual boundary;
        ``turn_loaded_end_seq`` and ``turn_complete`` expose budget-truncated
        payloads without misreporting their span. Pass ``include_turn=False``
        for the legacy matching-row-only shape. By default searches this agent
        across all its sessions. Pass ``all_agents=True`` to span every agent,
        or pin a *specific* conversation / agent with
        ``session_id='cron:<job>'`` and/or ``agent_id='<other>'`` (these
        AND-combine and take precedence). ``kind`` optionally filters by row
        kind. ``created_on`` restricts to one calendar date;
        ``created_from``/``created_to`` form an inclusive date range. These
        strict ISO date filters apply to ``created_at`` and may be used with an
        empty query for date-only recall. If matching content lives in a saved
        full tool-output file (because the history row only retained a
        truncated preview), search can return a ``tool_result`` row whose
        content is a small excerpt around the matching saved-file line, plus
        the file path. Artifact candidates are paged and files are streamed
        under a total byte/time budget; a ``_notice`` row explicitly reports
        when that fallback search is partial.

        The query is plain text: punctuation is treated as word separators (so
        ``C++`` searches the term ``C``). Whitespace-separated terms within one
        bare-uppercase-``OR`` alternative are AND-combined — so ``tank OR
        aquarium`` matches either word, while ``tank sunk`` needs both. Terms
        are matched case-insensitively as substrings, which is what makes
        keyword recall work for Chinese text whose words are not
        whitespace-delimited.

        The agent's current ACTIVE TURN (the latest user request of this
        session and everything after it) never appears in the hits — it is
        already in the live window, and matching it would only echo the
        previous recall round back. Earlier evicted turns of this session
        remain searchable.
        """
        requested = max(0, int(k))
        if requested == 0:
            return []
        targets = self._scope_filters(all_agents, session_id, agent_id)
        created_bounds = self._created_bounds(
            created_on=created_on,
            created_from=created_from,
            created_to=created_to,
        )

        def like_page(offset: int, page_limit: int) -> list[dict]:
            return self._search_like_rows(
                query,
                targets,
                kind,
                page_limit,
                offset=offset,
                created_bounds=created_bounds,
                deadline=_budget.deadline,
            )

        if query == "":
            def date_page(offset: int, page_limit: int) -> list[dict]:
                return self._search_date_rows(
                    targets,
                    kind,
                    page_limit,
                    offset=offset,
                    created_bounds=created_bounds,
                    deadline=_budget.deadline,
                )

            if not include_turn:
                return date_page(0, requested)
            rows, raw_partial, _ = self._collect_distinct_hits(
                date_page,
                requested,
                _budget,
            )
            return self._attach_search_turns(
                rows,
                requested,
                _budget,
                raw_hits_partial=raw_partial,
            )

        if not include_turn:
            rows = self._search_like_rows(
                query,
                targets,
                kind,
                requested,
                created_bounds=created_bounds,
                deadline=_budget.deadline,
            )
            # Search saved full tool outputs only after database hits are
            # exhausted, so a hit list is never padded by artifact scans.
            if kind in (None, "tool_result") and len(rows) < requested:
                rows.extend(
                    self._search_saved_tool_files(
                        query,
                        targets,
                        limit=requested - len(rows),
                        created_bounds=created_bounds,
                    ),
                )
            return rows[:requested]

        rows, raw_partial, distinct_count = self._collect_distinct_hits(
            like_page,
            requested,
            _budget,
        )
        if (
            not raw_partial
            and distinct_count < requested
            and kind in (None, "tool_result")
            and len(rows) < self._row_cap
        ):
            rows.extend(
                self._search_saved_tool_files(
                    query,
                    targets,
                    limit=max(0, self._row_cap - len(rows)),
                    created_bounds=created_bounds,
                ),
            )
        return self._attach_search_turns(
            rows,
            requested,
            _budget,
            raw_hits_partial=raw_partial,
        )

    def _collect_distinct_hits(
        self,
        fetch_page: Callable[[int, int], list[dict]],
        limit: int,
        budget: _SearchBudget,
    ) -> tuple[list[dict], bool, int]:
        """Page raw hits until ``limit`` distinct turns or a hard boundary.

        Returns ``(rows, raw_hits_partial, distinct_count)``. Fetching one
        extra row per page distinguishes exhaustion from the total raw-hit
        cap, so callers never silently present a cap-limited result as
        complete.
        """
        rows: list[dict] = []
        distinct: set[tuple[object, object, int]] = set()
        offset = 0
        has_more = False
        while len(distinct) < limit and offset < self._row_cap:
            budget.ensure_time()
            page_size = min(
                _SEARCH_HIT_PAGE_SIZE,
                self._row_cap - offset,
            )
            fetched = fetch_page(offset, page_size + 1)
            page = fetched[:page_size]
            has_more = len(fetched) > page_size
            if not page:
                has_more = False
                break
            rows.extend(page)
            starts = self._turn_start_seqs(page, budget)
            for row in page:
                if row.get("_truncated") or int(row.get("seq", -1)) < 0:
                    continue
                seq = int(row["seq"])
                start = starts.get(seq)
                distinct.add(
                    (
                        row.get("session_id"),
                        row.get("agent_id"),
                        start if start is not None else seq,
                    ),
                )
            offset += len(page)
            if not has_more:
                break
        raw_partial = (
            len(distinct) < limit and offset >= self._row_cap and has_more
        )
        return rows, raw_partial, len(distinct)

    @staticmethod
    def _real_user_conditions(prefix: str = "") -> tuple[list[str], list]:
        """SQL conditions identifying a kind-agnostic user boundary.

        Native Scroll rows use ``kind='context_msg'`` while imported history
        can use source-specific kinds such as ``beam_chat_turn``. The role is
        the portable boundary signal; tagged continuation stubs remain part
        of the preceding turn rather than opening a new one.
        """
        conditions = [f"{prefix}role = 'user'"]
        params: list = []
        for tag in _SYNTHETIC_USER_TAGS:
            conditions.append(
                f"({prefix}metadata IS NULL OR {prefix}metadata NOT LIKE %s)",
            )
            params.append(f'%"{tag}"%')
        return conditions, params

    def _turn_start_seqs(
        self,
        rows: list[dict],
        budget: _SearchBudget,
    ) -> dict[int, int | None]:
        """Resolve all hit-to-turn boundaries in small batched queries."""
        hit_seqs = list(
            dict.fromkeys(
                int(row["seq"])
                for row in rows
                if not row.get("_truncated") and int(row.get("seq", -1)) >= 0
            ),
        )
        starts: dict[int, int | None] = {}
        user_conditions, user_params = self._real_user_conditions("b.")
        for offset in range(0, len(hit_seqs), _TURN_BOUNDARY_BATCH_SIZE):
            budget.ensure_time()
            batch = hit_seqs[offset : offset + _TURN_BOUNDARY_BATCH_SIZE]
            placeholders = ", ".join("%s" for _ in batch)
            sql = (
                "SELECT hit.seq AS hit_seq, "
                f"(SELECT MAX(b.seq) FROM {_HISTORY_TABLE} b WHERE "
                "b.workspace_key = hit.workspace_key "
                "AND b.session_id = hit.session_id "
                "AND b.agent_id IS NOT DISTINCT FROM hit.agent_id "
                "AND "
                + " AND ".join(user_conditions)
                + " AND b.seq <= hit.seq) AS start_seq "
                f"FROM {_HISTORY_TABLE} hit "
                f"WHERE hit.workspace_key = %s AND hit.seq IN ({placeholders})"
            )
            # Parameter order follows the *text* order of the placeholders: the
            # correlated subquery's conditions come first, then the outer WHERE.
            fetched = _fetch(
                sql,
                (*user_params, self._workspace_key, *batch),
                timeout_ms=budget.remaining_ms(),
            )
            for row in fetched:
                starts[int(row["hit_seq"])] = (
                    int(row["start_seq"])
                    if row["start_seq"] is not None
                    else None
                )
        return starts

    def _load_turn(
        self,
        hit: dict,
        start_seq: int | None,
        budget: _SearchBudget,
    ) -> tuple[int, int, int | None, bool, list[dict]]:
        """Load one already-deduplicated turn under the shared budget."""
        budget.ensure_time()
        hit_seq = int(hit["seq"])
        session_id = hit.get("session_id")
        agent_id = hit.get("agent_id")
        lineage = [
            "workspace_key = %s",
            "session_id = %s",
            "agent_id IS NOT DISTINCT FROM %s",
        ]
        lineage_params: list = [self._workspace_key, session_id, agent_id]
        select_columns = (
            "seq, session_id, agent_id, kind, role, name, headline, "
            "content, blocks, tool_call_id, tool_input, tool_state, "
            "metadata, created_at"
        )

        if start_seq is None:
            # Legacy/imported rows may predate their user boundary. There is
            # no reliable way to pair such an orphan with neighbouring model
            # rows, so preserve the old row-only behaviour for this hit.
            effective_start = hit_seq
            actual_end = hit_seq
            span_conditions = ["workspace_key = %s", "seq = %s"]
            span_params: list = [self._workspace_key, hit_seq]
        else:
            effective_start = int(start_seq)
            user_conditions, user_params = self._real_user_conditions()
            next_rows = _fetch(
                "SELECT MIN(seq) AS seq "
                f"FROM {_HISTORY_TABLE} WHERE "
                + " AND ".join([*lineage, *user_conditions, "seq > %s"]),
                (*lineage_params, *user_params, int(start_seq)),
                timeout_ms=budget.remaining_ms(),
            )
            next_user_seq = (
                next_rows[0]["seq"]
                if next_rows and next_rows[0]["seq"] is not None
                else None
            )
            span_conditions = [*lineage, "seq >= %s"]
            span_params = [*lineage_params, int(start_seq)]
            if next_user_seq is not None:
                span_conditions.append("seq < %s")
                span_params.append(int(next_user_seq))
            end_rows = _fetch(
                "SELECT MAX(seq) AS seq "
                f"FROM {_HISTORY_TABLE} WHERE "
                + " AND ".join(span_conditions),
                tuple(span_params),
                timeout_ms=budget.remaining_ms(),
            )
            actual_end = (
                int(end_rows[0]["seq"])
                if end_rows and end_rows[0]["seq"] is not None
                else hit_seq
            )

        loaded = self._query(
            f"SELECT {select_columns} "
            f"FROM {_HISTORY_TABLE} WHERE "
            + " AND ".join(span_conditions)
            + " ORDER BY seq",
            tuple(span_params),
            deadline=budget.deadline,
        )

        turn: list[dict] = []
        for row in loaded:
            budget.ensure_time()
            shaped = dict(row)
            if not budget.consume_turn_row(shaped):
                turn.append(
                    {
                        "_truncated": True,
                        "_row_cap": budget.max_rows,
                        "_reason": budget.exhausted_reason,
                    },
                )
                break
            turn.append(shaped)
        real_rows = [row for row in turn if not row.get("_truncated")]
        if not real_rows:
            if not turn:
                return hit_seq, hit_seq, hit_seq, True, [dict(hit)]
            return effective_start, actual_end, None, False, turn
        loaded_end = int(real_rows[-1]["seq"])
        complete = loaded_end == actual_end and all(
            not row.get("_truncated") for row in turn
        )
        return (
            effective_start,
            actual_end,
            loaded_end,
            complete,
            turn,
        )

    def _attach_search_turns(
        self,
        rows: list[dict],
        limit: int,
        budget: _SearchBudget,
        *,
        raw_hits_partial: bool = False,
    ) -> list[dict]:
        """Deduplicate boundaries first, then load each selected turn once."""
        starts = self._turn_start_seqs(rows, budget)
        notices: list[dict] = []
        groups: dict[tuple[object, object, int], dict] = {}
        for row in rows:
            if row.get("_truncated") or int(row.get("seq", -1)) < 0:
                notices.append(row)
                continue
            seq = int(row["seq"])
            start = starts.get(seq)
            key = (
                row.get("session_id"),
                row.get("agent_id"),
                start if start is not None else seq,
            )
            existing = groups.get(key)
            if existing is not None:
                if seq not in existing["matched_seqs"]:
                    existing["matched_seqs"].append(seq)
                continue
            if len(groups) >= limit:
                continue
            groups[key] = {
                "hit": row,
                "start_seq": start,
                "matched_seqs": [seq],
            }

        results = list(notices)
        loaded_groups = 0
        for group in groups.values():
            if not budget.has_turn_capacity():
                break
            row = group["hit"]
            start, end, loaded_end, complete, turn = self._load_turn(
                row,
                group["start_seq"],
                budget,
            )
            result = dict(row)
            result.update(
                {
                    "match_seq": int(row["seq"]),
                    "matched_seqs": sorted(group["matched_seqs"]),
                    "turn_start_seq": start,
                    "turn_end_seq": end,
                    "turn_loaded_end_seq": loaded_end,
                    "turn_complete": complete,
                    "turn": turn,
                },
            )
            results.append(result)
            loaded_groups += 1
            if budget.exhausted_reason is not None:
                break
        if loaded_groups < len(groups) or budget.exhausted_reason is not None:
            results.append(self._turn_budget_notice(budget))
        if raw_hits_partial:
            results.append(self._raw_hit_cap_notice())
        return results

    @staticmethod
    def _turn_budget_notice(budget: _SearchBudget) -> dict:
        """Report partial turn expansion instead of silently over-consuming."""
        reason = budget.exhausted_reason or "turn expansion budget"
        return {
            "seq": -1,
            "session_id": None,
            "kind": "_notice",
            "role": None,
            "name": None,
            "headline": "search turn expansion was partial",
            "created_at": None,
            "content": (
                f"NOTE: complete-turn expansion reached its {reason} "
                f"({budget.max_rows} rows / {budget.max_bytes} bytes total). "
                "Results are partial; narrow the query or lower k."
            ),
        }

    def _raw_hit_cap_notice(self) -> dict:
        """Flag that raw-hit paging stopped before finding ``k`` turns."""
        return {
            "seq": -1,
            "session_id": None,
            "kind": "_notice",
            "role": None,
            "name": None,
            "headline": "search hit collection was partial",
            "created_at": None,
            "content": (
                "NOTE: search inspected the maximum "
                f"{self._row_cap} matching rows before collecting the "
                "requested number of distinct turns. Results are partial; "
                "narrow the query."
            ),
        }

    def _search_like_rows(
        self,
        query: str,
        targets: list[tuple[str, str]],
        kind: str | None,
        limit: int,
        *,
        offset: int = 0,
        created_bounds: tuple[str | None, str | None] = (None, None),
        deadline: float | None = None,
    ) -> list[dict]:
        """Return one stable page matching AND terms in any OR group."""
        groups = _like_search_groups(query)
        query_clauses: list[str] = []
        query_params: list[str] = []
        for terms in groups:
            query_clauses.append(
                "("
                + " AND ".join("content ILIKE %s ESCAPE '\\'" for _ in terms)
                + ")",
            )
            query_params.extend(map(_like_pattern, terms))
        # Exclude the recall tool's own turns (NULL-safe: keep un-named rows).
        where = [
            "workspace_key = %s",
            "(" + " OR ".join(query_clauses) + ")",
            f"(name IS NULL OR name NOT IN ({_RECALL_EXCL_PLACEHOLDERS}))",
        ]
        params: list = [
            self._workspace_key,
            *query_params,
            *_RECALL_TOOL_NAMES,
        ]
        excl = self._active_turn_exclusion()
        if excl:
            where.append(excl[0])
            params.extend(excl[1])
        for col, val in targets:
            where.append(f"{col} = %s")
            params.append(val)
        created_where, created_params = self._created_conditions(
            created_bounds,
        )
        where.extend(created_where)
        params.extend(created_params)
        if kind:
            where.append("kind = %s")
            params.append(kind)
        sql = (
            "SELECT seq, session_id, agent_id, kind, role, name, headline, "
            "content, metadata, created_at "
            f"FROM {_HISTORY_TABLE} "
            "WHERE "
            + " AND ".join(where)
            + " ORDER BY seq DESC LIMIT %s OFFSET %s"
        )
        params.extend((int(limit), int(offset)))
        return self._query(sql, params, deadline=deadline)

    def _search_date_rows(
        self,
        targets: list[tuple[str, str]],
        kind: str | None,
        limit: int,
        *,
        offset: int = 0,
        created_bounds: tuple[str | None, str | None] = (None, None),
        deadline: float | None = None,
    ) -> list[dict]:
        """Return a date-only page without requiring textual ``content``."""
        where = [
            "workspace_key = %s",
            f"(name IS NULL OR name NOT IN ({_RECALL_EXCL_PLACEHOLDERS}))",
        ]
        params: list = [self._workspace_key, *_RECALL_TOOL_NAMES]
        excl = self._active_turn_exclusion()
        if excl:
            where.append(excl[0])
            params.extend(excl[1])
        for col, val in targets:
            where.append(f"{col} = %s")
            params.append(val)
        created_where, created_params = self._created_conditions(
            created_bounds,
        )
        where.extend(created_where)
        params.extend(created_params)
        if kind:
            where.append("kind = %s")
            params.append(kind)
        sql = (
            "SELECT seq, session_id, agent_id, kind, role, name, headline, "
            "content, metadata, created_at "
            f"FROM {_HISTORY_TABLE} WHERE "
            + " AND ".join(where)
            + " ORDER BY seq DESC LIMIT %s OFFSET %s"
        )
        params.extend((int(limit), int(offset)))
        return self._query(sql, params, deadline=deadline)

    def _saved_tool_candidates(
        self,
        targets: list[tuple[str, str]],
        *,
        limit: int = _SAVED_TOOL_CANDIDATE_PAGE_SIZE,
        before_seq: int | None = None,
        created_bounds: tuple[str | None, str | None] = (None, None),
        deadline: float | None = None,
    ) -> list[dict]:
        """Tool-result rows whose truncated preview points at a saved file."""
        where = [
            "workspace_key = %s",
            "kind = 'tool_result'",
            # Doubled ``%`` because psycopg2 interpolates the statement whenever
            # parameters are bound, so a literal percent must be escaped.
            "(content LIKE '%%call `read_file` with file_path=%%' OR "
            "metadata LIKE '%%\"file_path\"%%')",
            f"(name IS NULL OR name NOT IN ({_RECALL_EXCL_PLACEHOLDERS}))",
        ]
        params: list = [self._workspace_key, *_RECALL_TOOL_NAMES]
        excl = self._active_turn_exclusion()
        if excl:
            where.append(excl[0])
            params.extend(excl[1])
        for col, val in targets:
            where.append(f"{col} = %s")
            params.append(val)
        created_where, created_params = self._created_conditions(
            created_bounds,
        )
        where.extend(created_where)
        params.extend(created_params)
        if before_seq is not None:
            where.append("seq < %s")
            params.append(int(before_seq))
        sql = (
            "SELECT seq, session_id, agent_id, kind, role, name, headline, "
            "tool_call_id, content, metadata, created_at "
            f"FROM {_HISTORY_TABLE} "
            "WHERE " + " AND ".join(where) + " ORDER BY seq DESC LIMIT %s"
        )
        params.append(int(limit))
        return self._query(sql, params, deadline=deadline)

    def _search_saved_tool_files(  # pylint: disable=too-many-branches
        self,
        query: str,
        targets: list[tuple[str, str]],
        *,
        limit: int,
        created_bounds: tuple[str | None, str | None] = (None, None),
    ) -> list[dict]:
        """Search full saved tool-result files referenced by history rows."""
        if limit <= 0:
            return []
        needle_groups = self._query_needle_groups(query)
        if not needle_groups:
            return []
        rows: list[dict] = []
        seen: set[tuple[int, str, int]] = set()
        budget = self._new_saved_tool_scan_budget()
        before_seq: int | None = None
        while len(rows) < limit and not budget.is_exhausted():
            candidates = self._saved_tool_candidates(
                targets,
                before_seq=before_seq,
                created_bounds=created_bounds,
                deadline=budget.deadline,
            )
            if not candidates:
                break
            next_before_seq = min(
                int(candidate["seq"]) for candidate in candidates
            )
            for row in candidates:
                if budget.is_exhausted():
                    break
                for path in self._saved_tool_paths(
                    row.get("content"),
                    row.get("metadata"),
                ):
                    if budget.is_exhausted():
                        break
                    matches = self._file_line_matches(
                        path,
                        needle_groups,
                        budget=budget,
                    )
                    for match in matches:
                        key = (
                            int(row["seq"]),
                            str(path),
                            int(match["line"]),
                        )
                        if key in seen:
                            continue
                        seen.add(key)
                        rows.append(
                            {
                                "seq": row["seq"],
                                "session_id": row.get("session_id"),
                                "agent_id": row.get("agent_id"),
                                "kind": row.get("kind"),
                                "role": row.get("role"),
                                "name": row.get("name"),
                                "created_at": row.get("created_at"),
                                "headline": (
                                    "saved tool output match at "
                                    f"{path.name}:{match['line']}"
                                ),
                                "content": (
                                    f"[saved tool output match]\n"
                                    f"tool_call_id="
                                    f"{row.get('tool_call_id') or ''}\n"
                                    f"file_path={path}\n"
                                    f"line={match['line']}\n"
                                    f"{match['excerpt']}"
                                ),
                            },
                        )
                        if len(rows) >= limit:
                            return rows
                    if budget.is_exhausted():
                        break
            before_seq = next_before_seq
            if len(candidates) < _SAVED_TOOL_CANDIDATE_PAGE_SIZE:
                break
        if budget.is_exhausted() and len(rows) < limit:
            rows.append(self._artifact_scan_notice())
        return rows

    def _attach_saved_tool_file_matches(
        self,
        rows: list[dict],
        query: str,
    ) -> list[dict]:
        """Annotate recall_tool rows with saved-file metadata when present."""
        needle_groups = self._query_needle_groups(query)
        out: list[dict] = []
        budget = self._new_saved_tool_scan_budget()
        for row in rows:
            raw_refs = self._raw_saved_tool_refs(
                row.get("content"),
                row.get("metadata"),
            )
            saved_refs = self._saved_tool_refs(
                row.get("content"),
                row.get("metadata"),
            )
            if raw_refs and not saved_refs:
                out.append(
                    {
                        "seq": row.get("seq"),
                        "kind": "_saved_tool_output_unavailable",
                        "role": None,
                        "name": row.get("name"),
                        "tool_input": None,
                        "tool_state": row.get("tool_state"),
                        "content": (
                            "ARTIFACT_UNAVAILABLE — the complete saved tool "
                            "output has expired, was deleted, or cannot be "
                            "accessed safely. The bounded preview retained "
                            "in Scroll history is returned below and may "
                            "not contain the full original result."
                        ),
                    },
                )
            for path, info in saved_refs:
                start_line = info.get("start_line") or 1
                extra = {
                    "seq": row.get("seq"),
                    "kind": "_saved_tool_output",
                    "role": None,
                    "name": row.get("name"),
                    "tool_input": None,
                    "tool_state": row.get("tool_state"),
                    "content": (
                        "Full saved tool output is available at "
                        f"file_path={str(path)!r} start_line={start_line}."
                    ),
                }
                if needle_groups and not budget.is_exhausted():
                    matches = self._file_line_matches(
                        path,
                        needle_groups,
                        limit=3,
                        budget=budget,
                    )
                    if matches:
                        content = str(extra["content"])
                        extra["content"] = (
                            content
                            + "\n\n"
                            + "\n\n".join(
                                f"match line {m['line']}:\n{m['excerpt']}"
                                for m in matches
                            )
                        )
                out.append(extra)
                if budget.is_exhausted():
                    # Preserve the durable bounded preview even when scanning
                    # the full artifact runs out of time or bytes. The
                    # pointer and partial-scan notice are supplemental rows;
                    # neither may replace the history row itself.
                    out.append(row)
                    out.append(self._artifact_scan_notice())
                    return out
            out.append(row)
        return out

    def _new_saved_tool_scan_budget(self) -> _ScanBudget:
        return _ScanBudget(
            remaining=self._saved_tool_scan_max_bytes,
            deadline=time.monotonic() + self._saved_tool_scan_max_seconds,
        )

    def _saved_tool_paths(
        self,
        content: object,
        metadata: object = None,
    ) -> list[Path]:
        """Extract saved artifact paths from metadata, then legacy notices."""
        return [path for path, _ in self._saved_tool_refs(content, metadata)]

    def _saved_tool_refs(
        self,
        content: object,
        metadata: object = None,
    ) -> list[tuple[Path, dict]]:
        """Return validated paths with structured continuation details."""
        if self._history_root is None:
            return []
        try:
            root = self._history_root.resolve()
        except OSError:
            return []
        refs: list[tuple[Path, dict]] = []
        seen: set[Path] = set()
        for raw_path, info in self._raw_saved_tool_refs(content, metadata):
            path = self._resolve_saved_tool_path(raw_path, root)
            if path is None or path in seen:
                continue
            try:
                if not path.is_file():
                    continue
            except OSError:
                # is_file() may raise ENAMETOOLONG / EACCES on oversized paths
                continue
            seen.add(path)
            refs.append((path, info))
        return refs

    @staticmethod
    def _raw_saved_tool_refs(
        content: object,
        metadata: object,
    ) -> list[tuple[str, dict]]:
        """Extract untrusted path/details pairs from metadata and notices."""
        raw_refs: list[tuple[str, dict]] = []
        parsed = metadata
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except (TypeError, ValueError):
                parsed = None
        if isinstance(parsed, dict):
            by_block = parsed.get("qwenpaw_truncation")
            if isinstance(by_block, dict):
                for info in by_block.values():
                    if isinstance(info, dict) and info.get("file_path"):
                        raw_refs.append((str(info["file_path"]), dict(info)))
        for match in _SAVED_TOOL_FILE_RE.finditer(str(content or "")):
            raw_path = match.group("quoted") or match.group("legacy")
            if raw_path:
                raw_refs.append(
                    (
                        raw_path,
                        {"start_line": int(match.group("start_line"))},
                    ),
                )
        return raw_refs

    @staticmethod
    def _resolve_saved_tool_path(raw_path: str, root: Path) -> Path | None:
        """Resolve one artifact path and reject traversal outside history."""
        if not raw_path:
            return None
        try:
            path = Path(raw_path).expanduser().resolve()
            path.relative_to(root)
        except (OSError, ValueError):
            return None
        return path

    @staticmethod
    def _query_needle_groups(query: str) -> list[list[str]]:
        """Saved-file terms grouped as AND arms joined by uppercase OR."""
        groups: list[list[str]] = []
        for raw_group in _or_query_groups(query):
            needles = [
                tok.casefold()
                for tok in _WORD_RE.findall(raw_group)
                if tok not in _SEARCH_OPERATORS
            ]
            # An empty arm must never become ``all([]) == True`` and match
            # every artifact line. Keep punctuation-only/malformed searches
            # restrictive, matching the old empty-needle behavior.
            if not needles:
                return []
            groups.append(needles)
        return groups

    @staticmethod
    def _file_line_matches(  # pylint: disable=too-many-branches
        path: Path,
        needle_groups: list[list[str]],
        *,
        limit: int = 5,
        context: int = 1,
        budget: _ScanBudget | None = None,
    ) -> list[dict]:
        """Stream line matches with bounded memory and byte consumption."""
        if (
            not needle_groups
            or any(not group for group in needle_groups)
            or limit <= 0
        ):
            return []
        if budget is None:
            budget = _ScanBudget(
                remaining=_SAVED_TOOL_SCAN_MAX_BYTES,
                deadline=time.monotonic() + _SAVED_TOOL_SCAN_MAX_SECONDS,
            )
        matches: list[dict] = []
        previous = deque(maxlen=max(0, context))
        pending: list[dict] = []
        try:
            file_obj = path.open("rb")
        except OSError:
            return []
        with file_obj:
            line_no = 0
            while len(matches) < limit:
                raw = budget.read_line(file_obj)
                if raw in (None, b""):
                    break
                line_no += 1
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")

                for item in list(pending):
                    item["lines"].append((line_no, line))
                    item["remaining"] -= 1
                    if item["remaining"] <= 0:
                        matches.append(
                            MemorySpace._finish_file_match(item),
                        )
                        pending.remove(item)
                if len(matches) >= limit:
                    break

                folded = line.casefold()
                if any(
                    all(needle in folded for needle in group)
                    for group in needle_groups
                ):
                    item = {
                        "line": line_no,
                        "lines": [*previous, (line_no, line)],
                        "remaining": max(0, context),
                    }
                    if context > 0:
                        pending.append(item)
                    else:
                        matches.append(MemorySpace._finish_file_match(item))
                previous.append((line_no, line))
                if budget.is_exhausted():
                    break
        for item in pending:
            if len(matches) >= limit:
                break
            matches.append(MemorySpace._finish_file_match(item))
        return matches

    @staticmethod
    def _finish_file_match(item: dict) -> dict:
        excerpt = "\n".join(
            f"{line_no}: {line}" for line_no, line in item["lines"]
        )
        return {"line": item["line"], "excerpt": excerpt}

    @staticmethod
    def _artifact_scan_notice() -> dict:
        """Flag that saved tool-output search stopped at its scan budget."""
        return {
            "seq": -1,
            "session_id": None,
            "kind": "_notice",
            "role": None,
            "name": None,
            "headline": "saved tool output search was partial",
            "content": (
                "NOTE: saved tool-output search reached its total scan byte "
                "or time budget. Results are partial; narrow the query or "
                "recall a specific tool_call_id/file_path."
            ),
        }

    def days_between(
        self,
        d1: object,
        d2: object,
        *,
        inclusive: bool = False,
    ) -> int:
        """Signed calendar-day difference ``d2 - d1`` for strict ISO values.

        Date and timestamp inputs share :func:`parse_date` semantics. Leap
        years are handled by :mod:`datetime`; invalid dates and malformed ISO
        values raise ``ValueError``. Pass ``inclusive=True`` to include both
        endpoints while preserving direction (``+1`` forward, ``-1``
        backward; equal dates return ``1``).
        """
        difference = (parse_date(d2) - parse_date(d1)).days
        if not inclusive:
            return difference
        return difference + 1 if difference >= 0 else difference - 1

    def close(self) -> None:
        """No-op kept for call-site symmetry.

        Connections are checked out per query now, so there is nothing to
        release; a MemorySpace holds no live database handle.
        """
        return None

    def __repr__(self) -> str:
        return f"<MemorySpace workspace={self._workspace_key[:12]}>"
