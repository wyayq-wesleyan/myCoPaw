from __future__ import annotations

import logging
import os
import threading
import time
import traceback
import uuid

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


class WorkerHeartbeat:
    """Keep liveness observable even while a parser blocks for minutes."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.worker_id = f"{os.uname().nodename}-{uuid.uuid4().hex[:8]}"
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
            self._touch()


def run() -> None:
    settings = Settings.from_env()
    settings.ensure_dirs()
    store = Store(settings.database_url)
    recovered = store.recover_interrupted_jobs()
    if recovered:
        logger.warning("recovered %d interrupted knowledge jobs", recovered)
    heartbeat = WorkerHeartbeat(store)
    heartbeat.start()
    logger.info("knowledge worker started; data_dir=%s", settings.data_dir)
    try:
        while True:
            job = store.claim_job()
            if job is None:
                heartbeat.set("idle")
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


if __name__ == "__main__":
    run()
