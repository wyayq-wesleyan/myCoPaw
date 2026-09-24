# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,protected-access,unused-argument
"""Unit tests for the durable :class:`HistoryStore` (PostgreSQL).

Covers append + idempotency (the unique-key net behind resume/migration),
in-place ``update_entry``, retention ``purge``, ``reconcile_session_rows``, the
degraded durability flag, and multi-threaded writes.

The store used to be a per-workspace SQLite file with an FTS5 index. Tests that
asserted on those mechanics — ``sqlite_master``, the ``conversation_history_fts``
virtual table, ``_fts`` availability, quarantine of a corrupt file — are gone
with the implementation. Keyword search is now a server-side trigram index, so
the search tests assert on *matching rows* rather than on index bookkeeping, and
the index's existence is asserted once from ``pg_indexes``.
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from qwenpaw.agents.context.scroll.history import HistoryStore
from qwenpaw.agents.context.types import LogEntry

from .conftest import history_indexes, history_rows


def _entry(content="hello", **kw) -> LogEntry:
    kw.setdefault("kind", "model_turn")
    kw.setdefault("role", "assistant")
    return LogEntry(content=content, **kw)


# --------------------------------------------------------------- append / seq


def test_append_assigns_increasing_seq_and_counts(store: HistoryStore):
    s1 = store.append(session_id="s", dedup_key="a", entry=_entry("one"))
    s2 = store.append(session_id="s", dedup_key="b", entry=_entry("two"))
    assert s2 > s1
    assert store.count("s") == 2
    assert store.count("other") == 0


def test_seq_starts_at_one_for_a_fresh_workspace(store: HistoryStore):
    """``seq`` is a per-workspace watermark, not a global identity.

    The eviction index persists ``seq`` ranges inside session state and rows are
    addressed by ``seq`` alone, so a workspace's numbering must not be affected by
    other workspaces writing to the same table.
    """
    assert store.append(session_id="s", dedup_key="a", entry=_entry()) == 1
    assert store.append(session_id="s", dedup_key="b", entry=_entry()) == 2


def test_two_workspaces_number_independently():
    """Two stores must not consume each other's sequence numbers."""
    from .conftest import requires_platform_db

    requires_platform_db.__wrapped__ if False else None
    first = HistoryStore("/tmp/qwenpaw-history-tests/ws-a/history.db")
    second = HistoryStore("/tmp/qwenpaw-history-tests/ws-b/history.db")
    try:
        assert first.append(session_id="s", dedup_key="a", entry=_entry()) == 1
        assert second.append(session_id="s", dedup_key="a", entry=_entry()) == 1
    finally:
        first.close()
        second.close()
        from .conftest import _delete_workspace

        _delete_workspace(first.workspace_key)
        _delete_workspace(second.workspace_key)


def test_append_is_idempotent_on_session_dedup_key(store: HistoryStore):
    """A second append of the same (session, dedup_key) is a no-op that returns
    the existing seq — the resume/migration safety net."""
    seq = store.append(session_id="s", dedup_key="m1", entry=_entry("first"))
    again = store.append(
        session_id="s",
        dedup_key="m1",
        entry=_entry("first again"),
    )
    assert again == seq
    assert store.count("s") == 1, "no duplicate row"


def test_null_dedup_key_is_never_deduped(store: HistoryStore):
    """A NULL key never conflicts — un-keyed rows are never deduped.

    ``ON CONFLICT`` treats SQL NULLs as distinct in a unique index, which is what
    makes a legitimate repeat write survive.
    """
    store.append(session_id="s", dedup_key=None, entry=_entry("x"))
    store.append(session_id="s", dedup_key=None, entry=_entry("x"))
    assert store.count("s") == 2


def test_same_dedup_key_different_session_does_not_collide(
    store: HistoryStore,
):
    a = store.append(session_id="s1", dedup_key="m1", entry=_entry())
    b = store.append(session_id="s2", dedup_key="m1", entry=_entry())
    assert a != b
    assert store.count("s1") == 1 and store.count("s2") == 1


def test_append_many_batches_and_deduplicates(store: HistoryStore):
    inserted = store.append_many(
        session_id="s",
        entries=[
            (_entry("one"), "m1"),
            (_entry("two"), "m2"),
            (_entry("duplicate"), "m1"),
        ],
    )
    assert inserted == 2
    assert store.count("s") == 2

    assert (
        store.append_many(
            session_id="s",
            entries=[(_entry("one again"), "m1")],
        )
        == 0
    )
    assert store.count("s") == 2


def test_append_many_assigns_consecutive_seqs(store: HistoryStore):
    store.append_many(
        session_id="s",
        entries=[(_entry("one"), "m1"), (_entry("two"), "m2")],
    )
    rows = history_rows(store, "s")
    assert [row["seq"] for row in rows] == [1, 2]


# ------------------------------------------------------------------ indexing


def test_indexes_for_recall_and_dedup_exist(store: HistoryStore):
    """The access paths the code relies on must be indexed.

    Replaces the old ``sqlite_master`` assertion: ``created_at`` is filtered by
    date-ranged recall, and the unique dedup index is the idempotency guarantee.
    """
    indexes = history_indexes(store)
    for expected in ("ch_created_at", "ux_dedup", "ch_session", "ch_content_trgm"):
        assert expected in indexes, f"{expected} missing from {sorted(indexes)}"


def test_keyword_index_matches_rows(store: HistoryStore):
    """The trigram index must actually serve substring matching.

    Search used to be exercised through the FTS5 ``MATCH`` operator. Asserting on
    matched rows keeps the test meaningful without depending on index internals.
    """
    store.append(
        session_id="s",
        dedup_key="m1",
        entry=_entry("the tanks rolled across the bridge"),
    )
    store.append(session_id="s", dedup_key="m2", entry=_entry("quiet meadow"))

    from qwenpaw.platform_store import get_conn, put_conn

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT seq FROM platform.conversation_history "
                "WHERE workspace_key = %s AND content ILIKE %s",
                (store.workspace_key, "%tank%"),
            )
            hits = [row[0] for row in cur.fetchall()]
    finally:
        put_conn(conn)

    assert len(hits) == 1, f"expected one match, got {hits}"


def test_content_change_is_visible_to_search_immediately(store: HistoryStore):
    """Editing a row must be reflected in keyword search with no index upkeep.

    SQLite's FTS5 needed explicit delete+insert on every update, and a missed one
    silently left stale terms searchable. PostgreSQL maintains the index itself,
    so this asserts the guarantee the old code had to hand-maintain.
    """
    seq = store.append(
        session_id="s", dedup_key="m1", entry=_entry("aardvark"),
    )
    store.update_entry(seq, content="zebra", headline=None, blocks=None)

    from qwenpaw.platform_store import get_conn, put_conn

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT content FROM platform.conversation_history "
                "WHERE workspace_key = %s AND content ILIKE %s",
                (store.workspace_key, "%aardvark%"),
            )
            stale = cur.fetchall()
            cur.execute(
                "SELECT content FROM platform.conversation_history "
                "WHERE workspace_key = %s AND content ILIKE %s",
                (store.workspace_key, "%zebra%"),
            )
            fresh = cur.fetchall()
    finally:
        put_conn(conn)

    assert stale == [], "the old term is still findable"
    assert len(fresh) == 1


# ------------------------------------------------------------------- update


def test_update_entry_refreshes_row_in_place(store: HistoryStore):
    seq = store.append(
        session_id="s",
        dedup_key="m1",
        entry=_entry("v1", headline=None),
    )
    store.update_entry(seq, content="v2", headline="grew", blocks=[{"t": 1}])

    rows = history_rows(store, "s")
    assert len(rows) == 1, "update must refresh, not insert"
    assert rows[0]["seq"] == seq
    assert rows[0]["content"] == "v2"
    assert rows[0]["headline"] == "grew"


def test_update_entry_refreshes_scalar_columns(store: HistoryStore):
    """A turn that grows a later tool call must not leave the scalar
    tool_call_id/name/tool_state frozen at their first-write values."""
    seq = store.append(
        session_id="s",
        dedup_key="m1",
        entry=_entry(
            "v1",
            tool_call_id="c1",
            name="grep",
            tool_state="running",
        ),
    )
    store.update_entry(
        seq,
        content="v2",
        headline=None,
        blocks=None,
        tool_call_id="c2",
        name="edit",
        tool_state="success",
    )
    row = history_rows(store, "s")[0]
    assert row["tool_call_id"] == "c2"
    assert row["name"] == "edit"
    assert row["tool_state"] == "success"


def test_update_entry_omitting_metadata_keeps_it(store: HistoryStore):
    """Older callers pass no ``metadata`` and must not have it cleared."""
    seq = store.append(
        session_id="s",
        dedup_key="m1",
        entry=_entry("v1", metadata={"keep": True}),
    )
    store.update_entry(seq, content="v2", headline=None, blocks=None)

    from qwenpaw.platform_store import get_conn, put_conn

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT metadata FROM platform.conversation_history "
                "WHERE workspace_key = %s AND seq = %s",
                (store.workspace_key, seq),
            )
            stored = cur.fetchone()[0]
    finally:
        put_conn(conn)

    assert stored and "keep" in stored


# --------------------------------------------------------------- transcript


def test_load_session_messages_orders_by_time_not_write_order(store: HistoryStore):
    """A turn written later but happening earlier must still sort first.

    ``seq`` is the *write* order and can disagree with the conversation's order —
    a resumed/retried turn, or rows imported by the sync pass, can land after a
    turn that chronologically follows it. Sorting by ``seq`` rendered a question
    *below* its answer.
    """
    store.append(
        session_id="s",
        dedup_key="later",
        entry=_entry("answer", role="assistant", created_at="2026-09-24T09:01:00"),
    )
    store.append(
        session_id="s",
        dedup_key="earlier",
        entry=_entry("question", role="user", created_at="2026-09-24T09:00:00"),
    )

    messages = store.load_session_messages("s")
    assert [m.get_text_content() for m in messages] == ["question", "answer"]


def test_load_session_messages_puts_undated_rows_last(store: HistoryStore):
    """A row with no timestamp trails the conversation instead of leading it.

    ``append`` always stamps ``created_at``, so an undated row only arrives from
    legacy data; it is written directly here for that reason. PostgreSQL's
    ``NULLS LAST`` keeps it at the end — the SQLite-side equivalent sorted ``NULL``
    first, which pushed such a row ahead of the whole conversation.
    """
    store.append(
        session_id="s",
        dedup_key="dated",
        entry=_entry("dated", created_at="2026-09-24T09:00:00"),
    )

    from qwenpaw.platform_store import get_conn, put_conn

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO platform.conversation_history "
                "(workspace_key, seq, session_id, kind, role, name, content, "
                " created_at, dedup_key) "
                "VALUES (%s, 2, 's', 'model_turn', 'assistant', NULL, "
                " 'undated', NULL, 'undated')",
                (store.workspace_key,),
            )
        conn.commit()
    finally:
        put_conn(conn)

    messages = store.load_session_messages("s")
    assert [m.get_text_content() for m in messages] == ["dated", "undated"]


def test_load_session_messages_carries_timestamps(store: HistoryStore):
    """The console reads a message time from ``metadata.timestamp``."""
    store.append(
        session_id="s",
        dedup_key="m1",
        entry=_entry("hello", created_at="2026-09-24T09:16:27"),
    )
    message = store.load_session_messages("s")[0]
    assert (message.metadata or {}).get("timestamp") == "2026-09-24T09:16:27"
    assert message.created_at == "2026-09-24T09:16:27"


def test_load_session_messages_drops_duplicates(store: HistoryStore):
    """The same event written under two keys must be returned once."""
    store.append(
        session_id="s", dedup_key="k1", entry=_entry("q", role="user",
        created_at="2026-09-24T09:00:00"),
    )
    store.append(
        session_id="s", dedup_key="k2", entry=_entry("a", role="assistant",
        created_at="2026-09-24T09:01:00"),
    )
    store.append(
        session_id="s", dedup_key="k3", entry=_entry("q", role="user",
        created_at="2026-09-24T09:00:00"),
    )

    messages = store.load_session_messages("s")
    assert [m.get_text_content() for m in messages] == ["q", "a"]


def test_load_session_messages_keeps_a_real_repeat(store: HistoryStore):
    """Asking the same thing again later is content, not a duplicate."""
    for key, text, role, when in (
        ("k1", "q", "user", "2026-09-24T09:00:00"),
        ("k2", "a", "assistant", "2026-09-24T09:01:00"),
        ("k3", "q", "user", "2026-09-24T10:00:00"),
        ("k4", "b", "assistant", "2026-09-24T10:01:00"),
    ):
        store.append(
            session_id="s",
            dedup_key=key,
            entry=_entry(text, role=role, created_at=when),
        )

    messages = store.load_session_messages("s")
    assert [m.get_text_content() for m in messages] == ["q", "a", "q", "b"]


def test_rebuilt_messages_have_stable_ids(store: HistoryStore):
    """Ids must not change between reads, or the console re-keys its cards."""
    store.append(session_id="s", dedup_key="m1", entry=_entry("stable"))
    first = store.load_session_messages("s")
    second = store.load_session_messages("s")
    assert first[0].id == second[0].id


def test_load_session_messages_filters_by_agent_but_keeps_null_agent(
    store: HistoryStore,
):
    """Legacy rows carry a NULL agent and must stay visible for their session."""
    store.append(
        session_id="s", agent_id=None, dedup_key="legacy", entry=_entry("legacy"),
    )
    store.append(
        session_id="s", agent_id="a1", dedup_key="owned", entry=_entry("owned"),
    )
    store.append(
        session_id="s", agent_id="a2", dedup_key="other", entry=_entry("other"),
    )

    messages = store.load_session_messages("s", "a1")
    assert sorted(m.get_text_content() for m in messages) == ["legacy", "owned"]


# ------------------------------------------------------- reconcile / claim


def test_reconcile_rows_uses_exact_file_keys_and_claims_agent(
    store: HistoryStore,
):
    selected_seq = store.append(
        session_id="sync:shared-stem",
        agent_id=None,
        dedup_key="selected",
        entry=_entry("selected row"),
    )
    unrelated_seq = store.append(
        session_id="sync:shared-stem",
        agent_id=None,
        dedup_key="other-file",
        entry=_entry("unrelated row"),
    )

    moved, deduplicated, claimed = store.reconcile_session_rows(
        {"sync:shared-stem"},
        "canonical",
        {"selected"},
        agent_id="agent-1",
    )

    assert (moved, deduplicated, claimed) == (1, 0, 0)
    moved_rows = history_rows(store, "canonical")
    assert len(moved_rows) == 1
    assert moved_rows[0]["seq"] == selected_seq
    assert moved_rows[0]["agent_id"] == "agent-1"

    # The unrelated row stayed put, untouched.
    remaining = history_rows(store, "sync:shared-stem")
    assert [row["seq"] for row in remaining] == [unrelated_seq]


def test_reconcile_rows_deduplicates_source(store: HistoryStore):
    kept = store.append(
        session_id="canonical",
        dedup_key="shared",
        entry=_entry("canonical copy"),
    )
    duplicate = store.append(
        session_id="sync:file",
        dedup_key="shared",
        entry=_entry("legacy duplicate"),
    )

    moved, deduplicated, claimed = store.reconcile_session_rows(
        {"sync:file"},
        "canonical",
        {"shared"},
    )

    assert (moved, deduplicated, claimed) == (0, 1, 0)
    assert store.count("sync:file") == 0
    assert store.count("canonical") == 1
    assert duplicate != kept


def test_claim_session_assigns_unowned_rows(store: HistoryStore):
    store.append(
        session_id="s", agent_id=None, dedup_key="m1", entry=_entry(),
    )
    claimed = store.claim_session("s", "a1")
    assert claimed == 1
    assert history_rows(store, "s")[0]["agent_id"] == "a1"
    # Idempotent: nothing left unowned.
    assert store.claim_session("s", "a1") == 0


# -------------------------------------------------------- seq-addressed reads


def test_existing_seqs_returns_only_present(store: HistoryStore):
    s1 = store.append(session_id="s", dedup_key="a", entry=_entry())
    s2 = store.append(session_id="s", dedup_key="b", entry=_entry())
    assert store.existing_seqs({s1, s2, 99999}) == {s1, s2}


def test_contents_by_seqs_returns_exact_rows(store: HistoryStore):
    s1 = store.append(session_id="s", dedup_key="a", entry=_entry("first"))
    store.append(session_id="s", dedup_key="b", entry=_entry("second"))
    assert store.contents_by_seqs({s1}) == {s1: "first"}
    assert store.contents_by_seqs(set()) == {}


def test_seq_addressed_reads_are_scoped_to_one_workspace(store: HistoryStore):
    """``seq`` repeats across workspaces, so a lookup must not cross them.

    Two workspaces both start at 1; addressing by ``seq`` alone would otherwise
    let one workspace read the other's row.
    """
    other = HistoryStore("/tmp/qwenpaw-history-tests/other-ws/history.db")
    try:
        store.append(session_id="s", dedup_key="a", entry=_entry("mine"))
        other.append(session_id="s", dedup_key="a", entry=_entry("theirs"))
        assert other.contents_by_seqs({1}) == {1: "theirs"}
    finally:
        other.close()
        from .conftest import _delete_workspace

        _delete_workspace(other.workspace_key)


# --------------------------------------------------------------------- purge


def test_purge_drops_old_rows_and_keeps_recent(store: HistoryStore):
    store.append(
        session_id="s",
        dedup_key="old",
        entry=_entry("ancient", created_at="2020-01-01T00:00:00+00:00"),
    )
    store.append(
        session_id="s",
        dedup_key="new",
        entry=_entry("fresh", created_at="2030-01-01T00:00:00+00:00"),
    )
    removed = store.purge(before="2025-01-01T00:00:00+00:00")
    assert removed == 1
    assert store.count("s") == 1


def test_purge_dry_run_reports_count_without_deleting(store: HistoryStore):
    store.append(
        session_id="s",
        dedup_key="old",
        entry=_entry("ancient", created_at="2020-01-01T00:00:00+00:00"),
    )
    store.append(
        session_id="s",
        dedup_key="new",
        entry=_entry("fresh", created_at="2030-01-01T00:00:00+00:00"),
    )
    would = store.purge(before="2025-01-01T00:00:00+00:00", dry_run=True)
    assert would == 1
    assert store.count("s") == 2, "nothing actually removed"
    assert store.purge(before="2025-01-01T00:00:00+00:00") == 1
    assert store.count("s") == 1


def test_estimate_purge_reports_rows_and_bytes(store: HistoryStore):
    store.append(
        session_id="s",
        dedup_key="old",
        entry=_entry("ancient", created_at="2020-01-01T00:00:00+00:00"),
    )
    store.append(
        session_id="s",
        dedup_key="new",
        entry=_entry("fresh", created_at="2030-01-01T00:00:00+00:00"),
    )
    est = store.estimate_purge(before="2025-01-01T00:00:00+00:00")
    assert est["rows"] == 1
    assert est["content_bytes"] == len("ancient")
    assert store.count("s") == 2, "estimating never deletes"


def test_purge_kinds_drops_only_tool_output(store: HistoryStore):
    old = "2020-01-01T00:00:00+00:00"
    store.append(
        session_id="s",
        dedup_key="turn",
        entry=_entry("conversation", kind="model_turn", created_at=old),
    )
    store.append(
        session_id="s",
        dedup_key="result",
        entry=_entry("big tool output", kind="tool_result", created_at=old),
    )
    est = store.estimate_purge(
        before="2025-01-01T00:00:00+00:00",
        kinds=("tool_result",),
    )
    assert est["rows"] == 1
    assert est["content_bytes"] == len("big tool output")

    removed = store.purge(
        before="2025-01-01T00:00:00+00:00",
        kinds=("tool_result",),
    )
    assert removed == 1
    assert store.count("s") == 1, "the conversation turn survives"


def test_purge_retains_null_created_at(store: HistoryStore):
    store.append(session_id="s", dedup_key="k", entry=_entry("no stamp"))
    from qwenpaw.platform_store import get_conn, put_conn

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE platform.conversation_history SET created_at = NULL "
                "WHERE workspace_key = %s AND session_id = 's'",
                (store.workspace_key,),
            )
        conn.commit()
    finally:
        put_conn(conn)

    removed = store.purge(before="2999-01-01T00:00:00+00:00")
    assert removed == 0
    assert store.count("s") == 1


def test_purge_is_scoped_to_the_workspace(store: HistoryStore):
    """A retention purge must not delete another workspace's history."""
    other = HistoryStore("/tmp/qwenpaw-history-tests/purge-other/history.db")
    try:
        old = "2020-01-01T00:00:00+00:00"
        store.append(
            session_id="s", dedup_key="mine",
            entry=_entry("mine", created_at=old),
        )
        other.append(
            session_id="s", dedup_key="theirs",
            entry=_entry("theirs", created_at=old),
        )
        assert store.purge(before="2025-01-01T00:00:00+00:00") == 1
        assert other.count("s") == 1, "another workspace was purged"
    finally:
        other.close()
        from .conftest import _delete_workspace

        _delete_workspace(other.workspace_key)


# ------------------------------------------------------------------- health


def test_note_write_failure_sets_degraded(store: HistoryStore):
    assert store.degraded is False
    store.note_write_failure(sqlite3.OperationalError("disk full"))
    assert store.degraded is True
    assert store.write_failures == 1
    store.note_write_failure(OSError("still bad"))
    assert store.write_failures == 2, "counted, stays degraded"


def test_vacuum_is_a_no_op(store: HistoryStore):
    """PostgreSQL reclaims space itself; the call must still be safe."""
    store.append(session_id="s", dedup_key="a", entry=_entry())
    store.vacuum()
    assert store.count("s") == 1


def test_close_is_idempotent(store: HistoryStore):
    assert store.closed is False
    store.close()
    store.close()
    assert store.closed is True


def test_workspace_key_is_stable_and_distinct(tmp_path: Path):
    from qwenpaw.agents.context.scroll.history import workspace_key_for

    a = workspace_key_for(tmp_path / "ws1" / "history.db")
    assert a == workspace_key_for(tmp_path / "ws1" / "history.db")
    assert a != workspace_key_for(tmp_path / "ws2" / "history.db")


# ---------------------------------------------------------------- threading


def test_append_works_from_a_worker_thread(store: HistoryStore):
    """The write-through in compress is offloaded via ``asyncio.to_thread``, so
    a pooled connection must be usable from another thread."""
    store.append(session_id="s", dedup_key="loop", entry=_entry("on-thread"))

    async def drive():
        return await asyncio.to_thread(
            store.append,
            session_id="s",
            dedup_key="worker",
            entry=_entry("off-thread"),
        )

    seq = asyncio.run(drive())
    assert seq > 0
    assert store.count("s") == 2


def test_concurrent_threaded_appends_get_distinct_seqs(store: HistoryStore):
    """Concurrent writers must not be handed the same ``seq``.

    The counter is advanced by one statement, so the allocation is atomic even
    when many worker threads append at once.
    """

    async def drive():
        return await asyncio.gather(
            *(
                asyncio.to_thread(
                    store.append,
                    session_id="s",
                    dedup_key=f"k{i}",
                    entry=_entry(f"row-{i}"),
                )
                for i in range(25)
            ),
        )

    seqs = asyncio.run(drive())

    assert store.count("s") == 25
    assert len(set(seqs)) == 25, f"duplicate seq allocated: {sorted(seqs)}"
