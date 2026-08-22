from __future__ import annotations

import logging
import time

from .config import Settings
from .processing import process_document
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s knowledge-worker %(message)s")
logger = logging.getLogger("knowledge-worker")


def run() -> None:
    settings = Settings.from_env()
    settings.ensure_dirs()
    store = Store(settings.db_path)
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
            logger.exception("failed document=%s", job["document_id"])
            store.fail_job(job["id"], job["document_id"], str(exc))


if __name__ == "__main__":
    run()
