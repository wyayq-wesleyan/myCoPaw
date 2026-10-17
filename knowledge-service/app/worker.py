from __future__ import annotations

import logging
import os
import threading
import time
import traceback
import uuid
from typing import Any

from .config import Settings
from .logging_setup import configure_logging
from .processing import process_document
from .store import Store

# Console + rotating file under KNOWLEDGE_LOG_DIR (bind-mounted to the host).
configure_logging("knowledge-worker")
logger = logging.getLogger("knowledge-worker")


# Three retries after the initial attempt (four total attempts).
MAX_ATTEMPTS = 4
HEARTBEAT_INTERVAL_SECONDS = 10

# Delay before retrying a loop iteration whose database call failed. A short
# outage (PostgreSQL restart, connection reset) must not take the worker down:
# this process has no supervisor of its own, and losing it makes uploads look
# broken to the user ("Worker 心跳已超时") until the container is restarted.
DB_RETRY_SECONDS = 5

# How often the idle branch reruns startup-style recovery. Jobs whose owner
# died without a restart (OOM kill of the worker process while the container
# stayed up, a crash mid-claim) are reclaimed once the heartbeat grace window
# passes — on this tick, not on the hour-scale stale sweep.
RECOVERY_SWEEP_INTERVAL_SECONDS = 60


def _resolve_worker_id() -> str:
    """Identity of this worker instance.

    Job recovery treats "owner's heartbeat went stale" as "owner is dead", and
    reclaims a restarting worker's own previous-life jobs immediately — both
    only hold if the id is **stable across restarts**. ``KNOWLEDGE_WORKER_ID``
    (set in Compose) is authoritative; the container hostname is the fallback,
    which survives ``docker restart`` but changes on recreate, in which case
    the periodic recovery sweep still picks the orphans up within about a
    minute of the old heartbeat going stale.
    """
    explicit = os.getenv("KNOWLEDGE_WORKER_ID", "").strip()
    if explicit:
        return explicit
    return os.uname().nodename


class LockHeldByAnotherInstance(RuntimeError):
    """The worker id is already owned by another live process (definitive)."""


class WorkerInstanceLock:
    """Session-level advisory lock that makes one worker id single-live.

    Recovery trusts "owner heartbeat stale ⇒ owner dead". That is only sound
    if two live processes can never share a worker id, which is exactly what
    this lock guarantees: a worker may start only while it holds the lock, and
    the main loop re-tests it so a dropped database connection (which releases
    the server-side lock) is noticed instead of silently leaving two instances
    under one id. The lock dies with the session, so a crash frees it for the
    next instance automatically.
    """

    _RECONNECT_ATTEMPTS = 5
    _RECONNECT_DELAY = 3.0

    def __init__(self, database_url: str, worker_id: str) -> None:
        import psycopg

        self._psycopg = psycopg
        self._url = database_url
        self._worker_id = worker_id
        self._conn: Any = None  # autocommit psycopg connection holding the lock

    def _lock_key(self) -> int:
        import hashlib

        # Deterministic per worker id; keep it inside signed 64-bit int4 range
        # so pg_advisory_lock accepts it directly.
        return int(hashlib.sha256(self._worker_id.encode()).hexdigest()[:16], 16) >> 1

    def _connect_and_lock(self) -> None:
        conn = self._psycopg.connect(self._url)
        conn.autocommit = True
        row = conn.execute(
            "SELECT pg_try_advisory_lock(%s)",
            (self._lock_key(),),
        ).fetchone()
        if not row or not row[0]:
            conn.close()
            raise LockHeldByAnotherInstance(
                f"另一个 knowledge worker 实例已持有相同身份 {self._worker_id!r}；"
                "多副本部署必须为每个副本设置不同的 KNOWLEDGE_WORKER_ID"
            )
        self._conn = conn

    def acquire(self, attempts: int = 5, delay: float = 3.0) -> bool:
        """Take the lock, retrying while the database itself is still coming up.

        A definitive ``LockHeldByAnotherInstance`` propagates immediately —
        retrying a genuinely owned id would only wait out the budget to end
        in the same answer.
        """
        for attempt in range(1, attempts + 1):
            try:
                self._connect_and_lock()
                return True
            except LockHeldByAnotherInstance:
                raise
            except Exception as exc:
                if attempt == attempts:
                    logger.error(
                        "could not acquire the worker instance lock after %d attempt(s): %s",
                        attempts, exc,
                    )
                    return False
                logger.warning(
                    "worker lock attempt %d/%d failed, retrying in %.0fs: %s",
                    attempt, attempts, delay, exc,
                )
                time.sleep(delay)
        return False

    def _discard_conn(self) -> None:
        try:
            if self._conn is not None and not self._conn.closed:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def still_held(self) -> bool:
        """Re-test the lock cheaply; re-acquire after a database reconnect.

        ``pg_try_advisory_lock`` is idempotent for the session that already
        holds it, so calling it again is a free liveness check. ``False`` means
        another live instance owns the id — the caller must stop.

        A database outage is NOT that: the lock dies with the connection, so
        an outage is a re-acquire opportunity, and a transient one would
        otherwise turn every Postgres restart into a worker exit loop (the
        heartbeat and claim paths deliberately ride such outages out).
        Connection-level failures therefore get the same bounded retry
        ``acquire()`` uses. If the database stays unreachable past that, this
        returns ``True`` (keep running): while the database is down this
        worker cannot claim anything either, so it cannot duplicate work, and
        the next retest — once connectivity returns — gets the definitive
        answer. Only an explicit "lock held elsewhere" outcome stops it.
        """
        for attempt in range(1, self._RECONNECT_ATTEMPTS + 1):
            try:
                if self._conn is None or self._conn.closed:
                    self._connect_and_lock()
                    return True
                row = self._conn.execute(
                    "SELECT pg_try_advisory_lock(%s)",
                    (self._lock_key(),),
                ).fetchone()
                return bool(row and row[0])
            except LockHeldByAnotherInstance:
                # Unambiguous: another live instance owns the id now.
                self._discard_conn()
                raise
            except Exception as exc:
                self._discard_conn()
                if attempt == self._RECONNECT_ATTEMPTS:
                    logger.error(
                        "could not verify the worker instance lock after %d "
                        "attempt(s) (%s); assuming ownership and riding out "
                        "the outage — claims will fail until the database "
                        "returns",
                        self._RECONNECT_ATTEMPTS, exc,
                    )
                    return True
                logger.warning(
                    "worker lock retest attempt %d/%d failed, retrying in "
                    "%.0fs: %s",
                    attempt, self._RECONNECT_ATTEMPTS, self._RECONNECT_DELAY,
                    exc,
                )
                time.sleep(self._RECONNECT_DELAY)
        return True

    def release(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None or conn.closed:
            return
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (self._lock_key(),))
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


class WorkerHeartbeat:
    """Keep liveness observable even while a parser blocks for minutes.

    ``worker_id`` MUST be the same stable id the claims run under
    (``WorkerInstanceLock``'s id): job recovery judges "abandoned" by the
    freshness of the row keyed ``jobs.owner_worker_id``. A heartbeat under a
    private random id would leave the owner's row frozen at claim time, so
    every recovery pass — this instance's own periodic sweep or another
    replica's — would see a live parse as dead after the 60s grace and
    requeue work that is actively running.
    """

    def __init__(self, store: Store, worker_id: str | None = None) -> None:
        self.store = store
        self.worker_id = worker_id or f"{os.uname().nodename}-{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._status = "idle"
        self._job_id: str | None = None
        self._stage: str | None = None
        self._thread = threading.Thread(target=self._run, name="knowledge-worker-heartbeat", daemon=True)

    def set(self, status: str, job_id: str | None = None, stage: str | None = None) -> None:
        with self._lock:
            self._status = status
            self._job_id = job_id
            self._stage = stage
        self._touch()

    def start(self) -> None:
        self._touch()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=HEARTBEAT_INTERVAL_SECONDS + 1)

    def _snapshot(self) -> tuple[str, str | None, str | None]:
        with self._lock:
            return self._status, self._job_id, self._stage

    def _touch(self) -> None:
        status, job_id, stage = self._snapshot()
        try:
            self.store.touch_worker_heartbeat(self.worker_id, status, job_id, stage)
        except Exception:
            logger.exception("failed to update knowledge worker heartbeat")

    def _run(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            try:
                self._touch()
            except Exception:
                # _touch already swallows store errors; this guards the loop
                # itself so an unexpected failure cannot silently stop the
                # heartbeat and make uploads look unavailable forever.
                logger.exception("heartbeat tick failed; continuing")


# How often to look for jobs wedged in "processing". Sweeping runs in the idle
# branch, which is hit on every poll; throttling keeps it from issuing a write
# query every few seconds forever.
STALE_SWEEP_INTERVAL_SECONDS = 300

_last_sweep_at = 0.0


def _stale_job_age_seconds(settings: Settings) -> int:
    """Age at which a silent "processing" job counts as stalled.

    Derived from the parser timeout rather than hard-coded: the parser is the
    longest legitimate single stage, and a threshold below it would reclaim
    healthy-but-slow work and duplicate it. The margin covers the remaining
    stages (fact extraction, embedding, graph indexing).
    """
    parser_timeout = int(getattr(settings, "mineru_timeout_seconds", 900) or 900)
    return max(1800, parser_timeout * 2 + 1800)


def _sweep_stale_jobs(store: Store, settings: Settings) -> None:
    """Reclaim jobs stuck in "processing". Never fatal to the worker loop."""
    global _last_sweep_at
    now = time.monotonic()
    if now - _last_sweep_at < STALE_SWEEP_INTERVAL_SECONDS:
        return
    _last_sweep_at = now
    try:
        reclaimed = store.requeue_stale_jobs(
            max_age_seconds=_stale_job_age_seconds(settings),
            max_attempts=MAX_ATTEMPTS,
        )
        if reclaimed:
            logger.warning("reclaimed %d stale knowledge job(s)", reclaimed)
    except Exception:
        logger.exception("stale job sweep failed; will retry next interval")


# The vector backfill converts a large existing corpus (hundreds of thousands of
# rows) and must not run on every idle tick, which fires every few seconds.
VECTOR_BACKFILL_INTERVAL_SECONDS = 300
_vector_backfill_state = {"last_at": 0.0, "done": False}


def _backfill_vectors_while_idle(store: Store) -> None:
    """Convert legacy ``vector_json`` rows, then build the HNSW index.

    Runs from the idle branch only, once per ``VECTOR_BACKFILL_INTERVAL_SECONDS``
    until the column is populated *and* indexed, then stops for the life of the
    process. This is the step that turns "chat is slow with 2000 documents" back
    into "chat is fast".

    The index is built here rather than at start-up because building it over a
    large corpus takes minutes, and start-up runs inside ``Store.__init__`` —
    before the service can answer anything. Doing it there made the console's
    health probe time out (503 on the knowledge page) until the build finished.

    Note the index step runs even when **nothing was converted**: a deployment
    whose rows were already backfilled — or which was upgraded while the backfill
    was mid-flight and then finished it — has an unindexed column and a zero
    conversion count, and gating on ``converted`` left it unindexed forever.

    Never fatal: a failure just means the next tick tries again, and retrieval
    keeps working through the column scan (and the jsonb fallback for any
    remaining rows).
    """
    if _vector_backfill_state["done"]:
        return
    now = time.monotonic()
    if now - _vector_backfill_state["last_at"] < VECTOR_BACKFILL_INTERVAL_SECONDS:
        return
    _vector_backfill_state["last_at"] = now

    try:
        converted = store.backfill_embedding_vectors_until_done()
    except Exception:
        logger.exception("pgvector backfill pass failed; will retry next interval")
        return

    remainder = store.count_unconverted_embeddings()
    if converted:
        logger.info(
            "pgvector backfill: converted %d embedding(s), %d remain",
            converted, remainder,
        )
    # ``-1`` means the count could not be taken (database unreachable). Treat it as
    # "not finished" so the next tick retries instead of latching the migration
    # as complete on a transient error.
    if remainder != 0:
        return

    # Nothing left to convert: make sure the index exists, then stop. The latch is
    # set only on success — marking it done after a failed build would leave the
    # column permanently unindexed while every tick short-circuits.
    try:
        indexed = store.rebuild_vector_index_if_needed()
    except Exception:
        logger.exception("pgvector index build failed; will retry next interval")
        return
    if not indexed:
        logger.info(
            "pgvector: index not ready yet (nothing pending, but the build did "
            "not complete); will retry",
        )
        return
    _vector_backfill_state["done"] = True
    logger.info("pgvector backfill: complete and indexed")


def run() -> None:
    settings = Settings.from_env()
    settings.ensure_dirs()
    store = Store(settings.database_url)
    worker_id = _resolve_worker_id()
    # The instance lock is a precondition, not a nicety: without it two live
    # processes could share one worker id, which would let each steal the
    # other's jobs and duplicate parsing, embeddings and graph writes.
    instance_lock = WorkerInstanceLock(settings.database_url, worker_id)
    try:
        if not instance_lock.acquire():
            raise SystemExit(
                f"无法取得 worker 实例锁（身份 {worker_id!r}）；"
                "多副本部署必须为每个副本设置不同的 KNOWLEDGE_WORKER_ID"
            )
    except LockHeldByAnotherInstance as exc:
        raise SystemExit(str(exc)) from exc
    # Startup recovery is best-effort. A database that is not reachable yet
    # (it may still be starting alongside this container) must not abort the
    # worker: the heartbeat below is what tells the API whether uploads are
    # possible, so dying here would report "worker not running" indefinitely.
    # reclaim_owner marks this instance's own previous life: the lock proves
    # the previous holder of this id is gone, so those jobs are reclaimed
    # immediately instead of waiting out the heartbeat grace window.
    try:
        recovered = store.recover_interrupted_jobs(reclaim_owner=worker_id)
        if recovered:
            logger.warning("recovered %d interrupted knowledge jobs", recovered)
    except Exception:
        logger.exception(
            "startup recovery failed; continuing so the heartbeat stays alive",
        )
    heartbeat = WorkerHeartbeat(store, worker_id=worker_id)
    heartbeat.start()
    logger.info(
        "knowledge worker started; data_dir=%s worker_id=%s",
        settings.data_dir, worker_id,
    )
    last_recovery_at = 0.0
    try:
        while True:
            try:
                # Raises LockHeldByAnotherInstance if a twin took the id now;
                # during a database outage it keeps returning True (claims
                # fail on their own until the database returns), so the worker
                # rides outages out instead of exiting.
                instance_lock.still_held()
                job = store.claim_job(worker_id=worker_id)
            except LockHeldByAnotherInstance as exc:
                # Definitive: another live instance owns this id now.
                raise SystemExit(str(exc)) from exc
            except SystemExit:
                raise
            except Exception:
                # Same reasoning as startup recovery: a transient database
                # error must not terminate the loop. Keep the heartbeat going
                # so the API reflects reality instead of a stale "unavailable".
                logger.exception("failed to claim a job; retrying")
                heartbeat.set("idle")
                time.sleep(DB_RETRY_SECONDS)
                continue
            if job is None:
                heartbeat.set("idle")
                # This loop is the only place a stalled job can be noticed, and
                # it is reached exactly when nothing is claimed — including when
                # a *previous* run left a job wedged in "processing". Sweeping
                # here reclaims it so the queue drains again; waiting for a
                # restart would leave uploads stuck at "等待 Worker" forever.
                _sweep_stale_jobs(store, settings)
                now = time.monotonic()
                if now - last_recovery_at >= RECOVERY_SWEEP_INTERVAL_SECONDS:
                    last_recovery_at = now
                    try:
                        orphaned = store.recover_interrupted_jobs()
                        if orphaned:
                            logger.warning(
                                "recovered %d orphaned knowledge job(s) (dead owner)",
                                orphaned,
                            )
                    except Exception:
                        logger.exception(
                            "orphan recovery failed; will retry next interval",
                        )
                # Backfill runs only while the queue is empty, so indexing work
                # never competes with an upload for the worker. It is resumable and
                # returns 0 once there is nothing left, which is the steady state.
                _backfill_vectors_while_idle(store)
                time.sleep(settings.worker_interval)
                continue

            def report(stage: str) -> None:
                store.update_job_stage(job["id"], job["document_id"], stage)
                heartbeat.set("processing", job["id"], stage)
                logger.info("document=%s stage=%s", job["document_id"], stage)

            try:
                report("parsing")
                process_document(store, settings, job, progress=report)
                store.finish_job(job["id"], job["document_id"])
                logger.info("completed document=%s", job["document_id"])
            except Exception as exc:  # worker must continue with later documents
                log_excerpt = traceback.format_exc()
                logger.error("failed document=%s (attempt %s)\n%s", job["document_id"], job.get("attempts"), log_excerpt)
                # Each failed attempt is delayed one hour; after three retries the
                # job remains failed and its log excerpt is downloadable.
                retry = (job.get("attempts") or 0) < MAX_ATTEMPTS
                try:
                    store.fail_job(job["id"], job["document_id"], log_excerpt, retry=retry)
                except Exception:
                    # A bookkeeping error must never kill the loop; the job stays
                    # in processing until the next start-up recovery pass.
                    logger.exception("failed to record failure for document=%s", job["document_id"])
                if retry:
                    logger.warning("scheduled document=%s for retry in 1 hour", job["document_id"])
            finally:
                heartbeat.set("idle")
    finally:
        heartbeat.stop()
        instance_lock.release()


if __name__ == "__main__":
    run()
