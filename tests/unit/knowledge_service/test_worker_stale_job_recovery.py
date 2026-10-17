# -*- coding: utf-8 -*-
"""A job wedged in "processing" must not block the queue forever.

Symptom reported from a deployment: an uploaded PDF sat at "等待 Worker"
(``processing_stage = 'queued'``) indefinitely while the LLM backend showed no
activity, i.e. nothing was being processed at all.

The cause was structural, not incidental. Ingest is single-threaded and the
only automatic recovery (``recover_interrupted_jobs``) ran **once, at worker
start-up**. So any run that hung *inside* a run — a parser that never returned,
a worker that died while the container stayed up and was restarted by compose —
left its row claimed in ``processing``. Every later document queued behind it
and never got claimed, because ``claim_job`` only ever looks at ``queued``.

Two properties are pinned here:

* a periodic sweep exists on the worker's idle path and requeues jobs whose
  ``processing`` state has gone silent past a threshold derived from the parser
  timeout;
* the threshold is *above* the longest legitimate stage, since sweeping a
  healthy-but-slow run would duplicate the work.

The SQL itself is exercised against PostgreSQL when the knowledge-service
environment is available (``KNOWLEDGE_TEST_DATABASE_URL`` + ``psycopg``);
otherwise the static guards still fail if the sweep is removed or disarmed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKER_PATH = REPO_ROOT / "knowledge-service" / "app" / "worker.py"
STORE_PATH = REPO_ROOT / "knowledge-service" / "app" / "store.py"

try:  # only present in the knowledge-service environment
    import psycopg  # noqa: F401

    _HAS_PSYCOPG = True
except ImportError:  # pragma: no cover - the default test env
    _HAS_PSYCOPG = False

PG_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL", "")


def _worker_source() -> str:
    return WORKER_PATH.read_text(encoding="utf-8")


def _store_method_body(name: str) -> str:
    source = STORE_PATH.read_text(encoding="utf-8")
    match = re.search(rf"\n    def {name}\(", source)
    assert match, f"Store.{name} not found in {STORE_PATH}"
    tail = source[match.end():]
    end = re.search(r"\n    (?:def |@)", tail)
    return tail[: end.start()] if end else tail


# ---------------------------------------------------------------------------
# Static guards — the sweep must exist, be armed, and be conservative
# ---------------------------------------------------------------------------


def test_a_stale_job_sweep_exists():
    """Its absence was the bug: start-up-only recovery never re-ran."""
    assert "requeue_stale_jobs" in STORE_PATH.read_text(encoding="utf-8")


def test_sweep_is_called_from_the_idle_path():
    """The idle branch is where a stalled predecessor is observable."""
    source = _worker_source()

    assert "_sweep_stale_jobs(store, settings)" in source, (
        "the worker no longer sweeps for stalled jobs"
    )
    # ...and specifically in the "nothing to claim" branch.
    idle_idx = source.index('heartbeat.set("idle")')
    sweep_idx = source.index("_sweep_stale_jobs(store, settings)")
    assert sweep_idx > idle_idx, (
        "the sweep must run when there is nothing to claim, otherwise a job "
        "wedged in 'processing' is never noticed"
    )


def test_sweep_is_throttled():
    """It runs on every idle poll; without throttling it writes constantly."""
    source = _worker_source()

    assert "STALE_SWEEP_INTERVAL_SECONDS" in source
    assert "_last_sweep_at" in source


def test_sweep_age_is_derived_from_the_parser_timeout():
    """A hard-coded short threshold would reclaim healthy-but-slow runs."""
    source = _worker_source()

    assert "mineru_timeout_seconds" in source, (
        "the stale threshold must scale with the parser timeout"
    )
    assert "max(1800," in source, "an absolute floor is required"


def test_sweep_only_touches_processing_jobs():
    """Queued jobs are the worker's input; the sweep must not disturb them."""
    body = _store_method_body("requeue_stale_jobs")

    assert "status = 'processing'" in body
    assert "updated_at < %s" in body, "staleness is judged by last update"


def test_exhausted_stale_jobs_are_failed_not_requeued_forever():
    """A reliably hanging document must eventually stop, not spin."""
    body = _store_method_body("requeue_stale_jobs")

    assert "attempts >= %s" in body
    assert "status = 'failed'" in body
    # The exhausted pass must run before the requeue, or it is undone by it.
    assert body.index("status = 'failed'") < body.rindex("status = 'queued'")


def test_worker_survives_a_database_error_while_claiming():
    """A transient DB error must not kill the heartbeat (uploads key off it)."""
    source = _worker_source()

    claim_idx = source.index("store.claim_job(")
    # The call must sit inside a try/except that logs and retries. The window
    # is generous because the surrounding branch order may change (e.g. a
    # definitive lock-takeover case ahead of the generic retry).
    window = source[max(0, claim_idx - 400): claim_idx + 1200]
    assert "except Exception" in window
    assert "DB_RETRY_SECONDS" in window


def test_startup_recovery_does_not_abort_the_worker():
    """PostgreSQL may simply not be up yet when the worker boots."""
    source = _worker_source()

    recover_idx = source.index("store.recover_interrupted_jobs(")
    window = source[max(0, recover_idx - 600): recover_idx]
    assert "try:" in window, (
        "start-up recovery must be best-effort; dying there reports the worker "
        "as unavailable until someone restarts it"
    )


def test_heartbeat_loop_tolerates_a_failing_tick():
    """A dead heartbeat thread makes uploads look permanently unavailable."""
    source = _worker_source()

    run_idx = source.index("def _run(self)")
    body = source[run_idx: run_idx + 600]
    assert "except Exception" in body


# ---------------------------------------------------------------------------
# Behaviour against a real database
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (_HAS_PSYCOPG and PG_URL),
    reason="needs psycopg and KNOWLEDGE_TEST_DATABASE_URL",
)
def test_stale_sweep_reclaims_a_wedged_job():
    """End-to-end: wedge a job, sweep it, and confirm it becomes claimable."""
    import sys
    import uuid
    from datetime import timedelta

    sys.path.insert(0, str(REPO_ROOT / "knowledge-service"))
    from app.store import Store, now_utc  # noqa: PLC0415

    store = Store(PG_URL)
    ts = now_utc()
    space_id = f"sweep-space-{uuid.uuid4().hex[:8]}"
    doc_id = f"sweep-doc-{uuid.uuid4().hex[:8]}"
    job_id = f"sweep-job-{uuid.uuid4().hex[:8]}"
    old = ts - timedelta(seconds=7200)

    try:
        with store._connect() as db:
            db.execute(
                "INSERT INTO spaces (id, owner_id, name, created_at) "
                "VALUES (%s, 'probe', 'sweep', %s)",
                (space_id, ts),
            )
            db.execute(
                "INSERT INTO documents (id, space_id, owner_id, filename, path, "
                "status, processing_stage, created_at, updated_at) "
                "VALUES (%s, %s, 'probe', 'x.pdf', '/tmp/x.pdf', "
                "'processing', 'parsing', %s, %s)",
                (doc_id, space_id, ts, ts),
            )
            db.execute(
                "INSERT INTO jobs (id, document_id, status, stage, attempts, "
                "created_at, updated_at) VALUES (%s, %s, 'processing', "
                "'parsing', 0, %s, %s)",
                (job_id, doc_id, ts, ts),
            )

        # A fresh run must be left alone — it may simply be slow.
        assert store.requeue_stale_jobs(max_age_seconds=600, max_attempts=4) == 0
        with store._connect() as db:
            row = db.execute(
                "SELECT status FROM jobs WHERE id = %s", (job_id,),
            ).fetchone()
        assert row["status"] == "processing"

        # Wedged: backdate the last update and sweep again.
        with store._connect() as db:
            db.execute("UPDATE jobs SET updated_at = %s WHERE id = %s", (old, job_id))
            db.execute(
                "UPDATE documents SET updated_at = %s WHERE id = %s",
                (old, doc_id),
            )

        assert store.requeue_stale_jobs(max_age_seconds=600, max_attempts=4) == 1
        with store._connect() as db:
            job = db.execute(
                "SELECT status, stage FROM jobs WHERE id = %s", (job_id,),
            ).fetchone()
            doc = db.execute(
                "SELECT status FROM documents WHERE id = %s", (doc_id,),
            ).fetchone()
        assert (job["status"], job["stage"]) == ("queued", "queued")
        assert doc["status"] == "queued", "the document view must agree"
    finally:
        with store._connect() as db:
            db.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
            db.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
            db.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
