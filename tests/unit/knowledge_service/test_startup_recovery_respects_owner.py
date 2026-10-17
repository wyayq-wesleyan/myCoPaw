# -*- coding: utf-8 -*-
"""Start-up recovery must not steal jobs a live worker is running.

``recover_interrupted_jobs`` used to requeue **every** row in ``processing``:

    UPDATE jobs SET status = 'queued' WHERE status = 'processing'

That is correct for a single worker (start-up means *the* worker died) but
wrong as soon as more than one runs — and ``claim_job`` already uses
``FOR UPDATE SKIP LOCKED``, i.e. concurrent consumers are supported. Any
restart would then yank the jobs surviving workers were mid-way through,
producing **duplicate parsing, duplicate embeddings and duplicate graph
writes** for the same document.

The final implementation decides ownership per **worker**: every claim records
``jobs.owner_worker_id`` plus a fresh heartbeat in the same transaction, and
recovery reclaims a job when its owner's heartbeat is stale (or immediately
for ``reclaim_owner`` — the caller restarting its own previous life, which a
session-level advisory lock makes safe). Matching heartbeat rows to *job ids*
is not sound: a heartbeat row is keyed by worker and refreshed every ~10s
regardless of which job it names, so "some worker heartbeated recently" would
make a fast restart leave a dead worker's job stranded until the hour-scale
stale sweep — which is the exact regression this file exists to prevent.

The SQL is exercised against PostgreSQL when the knowledge-service environment
is available (``KNOWLEDGE_TEST_DATABASE_URL`` + ``psycopg``); otherwise the
static guards still fail if the owner condition is removed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
STORE_PATH = REPO_ROOT / "knowledge-service" / "app" / "store.py"
WORKER_PATH = REPO_ROOT / "knowledge-service" / "app" / "worker.py"

try:  # only present in the knowledge-service environment
    import psycopg  # noqa: F401

    _HAS_PSYCOPG = True
except ImportError:  # pragma: no cover - the default test env
    _HAS_PSYCOPG = False

PG_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL", "")


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _method_body(source: str, name: str) -> str:
    match = re.search(rf"\n    def {name}\(", source)
    assert match, f"Store.{name} not found"
    tail = source[match.end():]
    end = re.search(r"\n    (?:def |@)", tail)
    return tail[: end.start()] if end else tail


# ---------------------------------------------------------------------------
# Static guards
# ---------------------------------------------------------------------------


def test_recovery_ownership_is_decided_by_the_owner_worker():
    """Heartbeat freshness must be judged per owner worker, not per job row."""
    body = _method_body(_source(STORE_PATH), "recover_interrupted_jobs")
    assert "owner_worker_id" in body, (
        "recovery must consult jobs.owner_worker_id; matching heartbeat rows "
        "to job ids leaves a dead worker's job stranded for as long as any "
        "surviving worker keeps heartbeating"
    )
    assert "worker_heartbeats" in body, (
        "start-up recovery does not consult worker heartbeats; a second "
        "worker restarting would requeue active jobs"
    )
    assert "NOT EXISTS" in body.upper(), (
        "the heartbeat check must exclude jobs owned by a live worker"
    )


def test_restart_reclaims_its_own_previous_life_immediately():
    """A fast container restart must not wait out the grace window (or worse,
    the hour-scale stale sweep) before reclaiming its own dead owner's jobs."""
    body = _method_body(_source(STORE_PATH), "recover_interrupted_jobs")
    assert "reclaim_owner" in body, (
        "recovery needs a reclaim_owner path for the restarting worker's own "
        "previous life, otherwise a restart inside the grace window strands "
        "its jobs for up to an hour"
    )


def test_claim_records_the_owner_and_heartbeat_in_one_transaction():
    """A claim without a heartbeat looks unowned — and stealable."""
    body = _method_body(_source(STORE_PATH), "claim_job")
    assert "owner_worker_id" in body, "claim must record the owner"
    assert "worker_heartbeats" in body, (
        "claim must write a fresh heartbeat in the same transaction; "
        "otherwise a recovery pass sees an unowned processing job"
    )


def test_recovery_only_resets_documents_it_actually_requeued():
    """A blanket documents UPDATE would show running documents as queued."""
    body = _method_body(_source(STORE_PATH), "recover_interrupted_jobs")
    assert "RETURNING id, document_id" in body, (
        "recovery must know which documents it reclaimed"
    )
    # The document reset must be driven by the reclaimed rows, not a second
    # unconditioned sweep over status = 'processing'.
    assert "for row in requeued" in body


def test_recovery_grace_is_several_heartbeat_intervals():
    """Too short a grace would reclaim healthy, merely slow work."""
    source = _source(STORE_PATH)
    match = re.search(r"RECOVERY_HEARTBEAT_GRACE_SECONDS\s*=\s*(\d+)", source)
    assert match, "RECOVERY_HEARTBEAT_GRACE_SECONDS constant is missing"
    grace = int(match.group(1))
    # The worker heartbeats every ~10s; the grace must tolerate several misses.
    assert grace >= 30, (
        f"grace of {grace}s is too tight to survive a missed tick"
    )


def test_worker_identity_is_stable_and_lock_guaranteed():
    """A random-per-process id makes owner-based recovery unsound."""
    source = _source(WORKER_PATH)
    assert "KNOWLEDGE_WORKER_ID" in source, (
        "the worker id must be configurable (and stable) per replica"
    )
    assert "pg_try_advisory_lock" in source, (
        "two live processes must never share one worker id; the session-level "
        "lock is what makes 'owner stale ⇒ owner dead' sound"
    )
    assert "still_held" in source, (
        "a dropped database connection silently releases the server-side "
        "lock; the main loop must re-test it"
    )


def test_heartbeat_thread_uses_the_stable_instance_id():
    """Recovery judges the freshness of the row keyed owner_worker_id — a
    heartbeat under a private random id would leave that row frozen at claim
    time, so every other replica's sweep would see live parses as dead."""
    source = _source(WORKER_PATH)
    assert "WorkerHeartbeat(store, worker_id=worker_id)" in source, (
        "the heartbeat thread must refresh the OWNER's heartbeat row (same "
        "stable id as the claims), not a private per-process one"
    )
    # And the default stays a safe fallback for other callers.
    assert "uuid.uuid4().hex[:8]" in source


def test_heartbeat_touches_the_owner_row_behaviourally():
    """Behavioural companion to the static guard above: the id handed to the
    store must be the stable one, whatever the source looks like."""
    pytest.importorskip("psycopg")  # app.worker imports the psycopg store
    import sys

    sys.path.insert(0, str(REPO_ROOT / "knowledge-service"))
    from app.worker import WorkerHeartbeat  # noqa: PLC0415

    touched: list[tuple[str, str, object, object]] = []

    class _FakeStore:
        def touch_worker_heartbeat(self, worker_id, status, job_id, stage):
            touched.append((worker_id, status, job_id, stage))

    heartbeat = WorkerHeartbeat(_FakeStore(), worker_id="owner-7")
    heartbeat.set("processing", "job-1", "parsing")

    assert touched, "set() must write a heartbeat"
    assert touched[-1][0] == "owner-7", (
        "the heartbeat row must be keyed by the stable owner id, or recovery "
        "sees the owner's row frozen at claim time"
    )


def test_lock_denial_is_definitive_not_retryable():
    """A twin holding the id must exit immediately; only connection errors
    may retry — otherwise every Postgres blip kills the worker."""
    source = _source(WORKER_PATH)
    assert "LockHeldByAnotherInstance" in source, (
        "'lock held elsewhere' must be a distinct, immediate failure — "
        "retrying it just burns the wait budget"
    )
    assert "_RECONNECT_ATTEMPTS" in source, (
        "a database outage releases the session lock silently; the retest "
        "must retry the reconnect instead of reading it as a twin takeover"
    )


# ---------------------------------------------------------------------------
# Behaviour against a real database
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (_HAS_PSYCOPG and PG_URL),
    reason="needs psycopg and KNOWLEDGE_TEST_DATABASE_URL",
)
def test_live_owned_job_is_left_alone_and_stale_one_is_reclaimed():
    """End-to-end: one live owner, one dead owner, one legacy row, one job
    owned by the restarting instance (fresh heartbeat, reclaim_owner set)."""
    import sys
    import uuid

    sys.path.insert(0, str(REPO_ROOT / "knowledge-service"))
    from app.store import Store, now_utc  # noqa: PLC0415
    from datetime import timedelta  # noqa: PLC0415

    store = Store(PG_URL)
    ts = now_utc()
    stale_ts = ts - timedelta(seconds=120)  # past the 60s grace
    suffix = uuid.uuid4().hex[:8]
    space_id = f"owner-space-{suffix}"
    names = {
        kind: f"owner-{kind}-{suffix}"
        for kind in ("live", "dead", "legacy", "mine")
    }
    live_doc = names["live"] + "-doc"
    dead_doc = names["dead"] + "-doc"
    legacy_doc = names["legacy"] + "-doc"
    mine_doc = names["mine"] + "-doc"
    docs = {
        "live": live_doc, "dead": dead_doc,
        "legacy": legacy_doc, "mine": mine_doc,
    }
    jobs = {kind: names[kind] + "-job" for kind in docs}
    live_worker = names["live"] + "-worker"
    dead_worker = names["dead"] + "-worker"
    restart_worker = names["mine"] + "-worker"

    try:
        with store._connect() as db:
            db.execute(
                "INSERT INTO spaces (id, owner_id, name, created_at) "
                "VALUES (%s, 'probe', 'owner', %s)",
                (space_id, ts),
            )
            rows = []
            for kind, doc_id in docs.items():
                job_updated = stale_ts if kind in ("dead", "legacy") else ts
                rows.append((docs[kind], jobs[kind], job_updated))
                db.execute(
                    "INSERT INTO documents (id, space_id, owner_id, filename, "
                    "path, status, processing_stage, created_at, updated_at) "
                    "VALUES (%s, %s, 'probe', 'x.pdf', '/tmp/x.pdf', "
                    "'processing', 'parsing', %s, %s)",
                    (doc_id, space_id, job_updated, job_updated),
                )
                db.execute(
                    "INSERT INTO jobs (id, document_id, status, stage, "
                    "attempts, owner_worker_id, created_at, updated_at) "
                    "VALUES (%s, %s, 'processing', 'parsing', 0, %s, %s, %s)",
                    (
                        jobs[kind], docs[kind],
                        # legacy rows predate owner recording
                        None if kind == "legacy" else {
                            "live": live_worker,
                            "dead": dead_worker,
                            "mine": restart_worker,
                        }[kind],
                        job_updated, job_updated,
                    ),
                )
            # Fresh heartbeat rows: the live owner is working, the "mine" row
            # is a stale leftover of the restarting instance's previous life.
            fresh_heartbeats = (
                (live_worker, jobs["live"]),
                (restart_worker, jobs["mine"]),
            )
            for worker_id, job_id in fresh_heartbeats:
                db.execute(
                    "INSERT INTO worker_heartbeats "
                    "(worker_id, status, job_id, stage, updated_at) "
                    "VALUES (%s, 'processing', %s, 'parsing', %s)",
                    (worker_id, job_id, ts),
                )
            # Dead owner's heartbeat: last touched 120s ago.

        reclaimed = store.recover_interrupted_jobs(
            reclaim_owner=restart_worker,
        )

        with store._connect() as db:
            job_statuses = {
                row["id"]: row["status"]
                for row in db.execute(
                    "SELECT id, status FROM jobs WHERE id = ANY(%s)",
                    ([jobs[k] for k in docs],),
                ).fetchall()
            }
            doc_statuses = {
                row["id"]: row["status"]
                for row in db.execute(
                    "SELECT id, status FROM documents WHERE id = ANY(%s)",
                    ([docs[k] for k in docs],),
                ).fetchall()
            }

        assert job_statuses[jobs["live"]] == "processing", (
            "a job held by a live worker was requeued — that duplicates work"
        )
        assert doc_statuses[live_doc] == "processing"
        assert job_statuses[jobs["dead"]] == "queued", (
            "an abandoned job (stale owner heartbeat) must be recovered"
        )
        assert doc_statuses[dead_doc] == "queued"
        assert job_statuses[jobs["legacy"]] == "queued", (
            "a legacy row with no owner and no fresh heartbeat must be "
            "recovered"
        )
        assert doc_statuses[legacy_doc] == "queued"
        assert job_statuses[jobs["mine"]] == "queued", (
            "the restarting worker must reclaim its own previous life's jobs "
            "immediately, even though the stale heartbeat row is fresh-ish"
        )
        assert doc_statuses[mine_doc] == "queued"
        assert reclaimed == 3, (
            f"expected exactly three reclaims, got {reclaimed}"
        )
    finally:
        with store._connect() as db:
            db.execute(
                "DELETE FROM worker_heartbeats WHERE worker_id = ANY(%s)",
                ([live_worker, dead_worker, restart_worker],),
            )
            db.execute(
                "DELETE FROM jobs WHERE id = ANY(%s)",
                ([jobs[k] for k in docs],),
            )
            db.execute(
                "DELETE FROM documents WHERE id = ANY(%s)",
                ([docs[k] for k in docs],),
            )
            db.execute("DELETE FROM spaces WHERE id = %s", (space_id,))


@pytest.mark.skipif(
    not (_HAS_PSYCOPG and PG_URL),
    reason="needs psycopg and KNOWLEDGE_TEST_DATABASE_URL",
)
def test_lock_retest_distinguishes_outage_from_twin_takeover():
    """A dropped connection must re-acquire (outage), not report the id lost;
    only a genuinely owned id ends the worker."""
    import sys
    import uuid

    sys.path.insert(0, str(REPO_ROOT / "knowledge-service"))
    from app.worker import (  # noqa: PLC0415
        LockHeldByAnotherInstance,
        WorkerInstanceLock,
    )

    worker_id = f"retest-lock-{uuid.uuid4().hex[:8]}"
    holder = WorkerInstanceLock(PG_URL, worker_id)
    assert holder.acquire(attempts=1)

    # Simulate the connection dropping (as on a Postgres restart): the
    # session lock is gone server-side and must be re-acquired, not read as
    # "someone else owns the id".
    holder._conn.close()
    assert holder.still_held() is True, (
        "a dropped connection must be ridden out, not treated as takeover"
    )

    # Now a twin grabs the id while holder is disconnected: the retest must
    # say so definitively. Close holder's connection first so the lock is
    # actually free for the twin.
    holder._conn.close()
    twin = WorkerInstanceLock(PG_URL, worker_id)
    assert twin.acquire(attempts=1)
    with pytest.raises(LockHeldByAnotherInstance):
        holder.still_held()
    holder.release()
    twin.release()


@pytest.mark.skipif(
    not (_HAS_PSYCOPG and PG_URL),
    reason="needs psycopg and KNOWLEDGE_TEST_DATABASE_URL",
)
def test_claim_writes_owner_and_fresh_heartbeat_atomically():
    """A claimed job must immediately be protected from any recovery pass."""
    import sys
    import uuid

    sys.path.insert(0, str(REPO_ROOT / "knowledge-service"))
    from app.store import Store  # noqa: PLC0415

    store = Store(PG_URL)
    suffix = uuid.uuid4().hex[:8]
    worker_id = f"claim-worker-{suffix}"

    try:
        created = store.create_document(
            None, "probe", f"claim-{suffix}.pdf", "/tmp/x.pdf",
            "application/pdf",
        )
        job_id = created["job_id"]

        claimed = store.claim_job(worker_id=worker_id)
        assert claimed and claimed["id"] == job_id

        # Any recovery pass right after the claim must leave it alone: the
        # owner is recorded and its heartbeat is fresh.
        reclaimed = store.recover_interrupted_jobs()
        assert reclaimed == 0, (
            "a just-claimed job was stolen by a concurrent recovery pass"
        )
        with store._connect() as db:
            job = db.execute(
                "SELECT owner_worker_id, status FROM jobs WHERE id = %s",
                (job_id,),
            ).fetchone()
            heartbeat = db.execute(
                "SELECT job_id, updated_at FROM worker_heartbeats "
                "WHERE worker_id = %s",
                (worker_id,),
            ).fetchone()
        assert job["owner_worker_id"] == worker_id
        assert job["status"] == "processing"
        assert heartbeat and heartbeat["job_id"] == job_id
    finally:
        with store._connect() as db:
            db.execute(
                "DELETE FROM worker_heartbeats WHERE worker_id = %s",
                (worker_id,),
            )
            db.execute("DELETE FROM jobs WHERE id = %s", (created["job_id"],))
            db.execute("DELETE FROM documents WHERE id = %s", (created["id"],))
