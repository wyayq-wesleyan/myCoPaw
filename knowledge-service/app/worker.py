from __future__ import annotations

import logging
import time

from .config import Settings
from .processing import process_document
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s knowledge-worker %(message)s")
logger = logging.getLogger("knowledge-worker")


MAX_JOB_ATTEMPTS = 2


def run() -> None:
    settings = Settings.from_env()
    settings.ensure_dirs()
    store = Store(settings.database_url)
    logger.info("knowledge worker started; data_dir=%s", settings.data_dir)
    while True:
        job = store.claim_job()
        if job is None:
            time.sleep(settings.worker_interval)
            continue
        try:
            logger.info("processing document=%s", job["document_id"])
            process_document(store, settings, job)
            store.finish_job(job["id"], job["document_id"])
            logger.info("completed document=%s", job["document_id"])
        except Exception as exc:  # worker must continue with later documents
            logger.exception("failed document=%s (attempt %s)", job["document_id"], job.get("attempts"))
            # 瞬时故障（模型加载超时等）自动重试一次，超过上限才标记失败
            if (job.get("attempts") or 1) < MAX_JOB_ATTEMPTS:
                store.requeue_job_for_retry(job["id"], job["document_id"])
                logger.warning("requeued document=%s for retry", job["document_id"])
                time.sleep(10)  # 退避，避免瞬时故障导致立即重试再失败
            else:
                store.fail_job(job["id"], job["document_id"], str(exc))


if __name__ == "__main__":
    run()
