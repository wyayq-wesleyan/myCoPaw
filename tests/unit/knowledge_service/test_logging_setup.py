# -*- coding: utf-8 -*-
"""Knowledge-service logs must reach the host, and SQL must be visible.

Two problems this pins:

1. The api / worker / lightrag containers had no log mount, so their output only
   existed in ``docker logs`` — which is unreachable once a container is recreated
   or its json-file log is rotated away. Diagnosing a failure meant having nothing
   to read. Each service now writes a rotating file under ``KNOWLEDGE_LOG_DIR``,
   bind-mounted to the host.

2. ``psycopg`` 3 does not log the SQL it executes at any level — only connection
   attempts and transactions — so "show me the query that ran" was impossible.
   ``store.LoggingConnection`` emits statements through the ``knowledge-sql``
   logger, switchable via ``KNOWLEDGE_SQL_LOG``.

The behavioural tests below need no database. The guard tests are static because
the wiring (pool construction, compose mounts) is what regresses.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
KNOWLEDGE_DIR = REPO_ROOT / "knowledge-service"

sys.path.insert(0, str(KNOWLEDGE_DIR))

import app.logging_setup as logging_setup  # noqa: E402


# --------------------------------------------------------------- file logging


@pytest.fixture
def log_env(tmp_path, monkeypatch):
    """Point the log directory at a temporary path and reset logging after."""
    monkeypatch.setenv("KNOWLEDGE_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("KNOWLEDGE_SQL_LOG", "true")
    monkeypatch.setenv("KNOWLEDGE_LOG_LEVEL", "INFO")
    root = logging.getLogger()
    before = list(root.handlers)
    yield tmp_path
    for handler in list(root.handlers):
        if handler not in before:
            handler.close()
    root.handlers[:] = before
    logging.getLogger("knowledge-sql").setLevel(logging.NOTSET)


def test_service_log_is_written_under_the_configured_directory(log_env) -> None:
    """``<service>.log`` must exist on the (host-mounted) directory."""
    handler = logging_setup.configure_logging("knowledge-api")
    try:
        logging.getLogger("knowledge-api").info("hello from the api")
        for h in logging.getLogger().handlers:
            h.flush()
        written = log_env / "knowledge-api.log"
        assert written.exists(), "no log file was created"
        assert "hello from the api" in written.read_text(encoding="utf-8")
    finally:
        if handler:
            handler.close()


def test_each_service_gets_its_own_file(log_env) -> None:
    """api and worker must not fight over one file."""
    a = logging_setup.configure_logging("knowledge-api")
    w = logging_setup.configure_logging("knowledge-worker")
    try:
        logging.getLogger("knowledge-api").info("api line")
        logging.getLogger("knowledge-worker").info("worker line")
        for h in logging.getLogger().handlers:
            h.flush()
        assert (log_env / "knowledge-api.log").exists()
        assert (log_env / "knowledge-worker.log").exists()
    finally:
        for h in (a, w):
            if h:
                h.close()


def test_rotation_is_bounded(log_env) -> None:
    """An unbounded log file would fill the host disk on a long-running worker."""
    handler = logging_setup.configure_logging("knowledge-api")
    try:
        assert handler is not None
        assert handler.maxBytes == logging_setup.MAX_BYTES
        assert handler.backupCount == logging_setup.BACKUP_COUNT
    finally:
        if handler:
            handler.close()


def test_unwritable_directory_degrades_to_console_only(tmp_path, monkeypatch) -> None:
    """A bad mount must not stop the service from starting."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("this is a file, not a directory")
    monkeypatch.setenv("KNOWLEDGE_LOG_DIR", str(blocker / "logs"))
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        handler = logging_setup.configure_logging("knowledge-api")
        assert handler is None, "should fall back to console-only"
    finally:
        for h in list(root.handlers):
            if h not in before:
                h.close()
        root.handlers[:] = before


def test_uvicorn_loggers_receive_the_file_handler(log_env) -> None:
    """uvicorn sets propagate=False, so its logs need the handler attached."""
    handler = logging_setup.configure_logging("knowledge-api")
    try:
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logging.getLogger(name).handlers.clear()
        logging_setup.attach_to_uvicorn(handler)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            assert handler in logging.getLogger(name).handlers
        # Idempotent: the lifespan can run more than once in tests.
        logging_setup.attach_to_uvicorn(handler)
        assert logging.getLogger("uvicorn").handlers.count(handler) == 1
    finally:
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logging.getLogger(name).handlers.clear()
        if handler:
            handler.close()


def test_attach_to_uvicorn_is_a_noop_without_a_handler() -> None:
    logging_setup.attach_to_uvicorn(None)


# -------------------------------------------------------------- SQL logging


def test_sql_log_level_follows_the_switch(monkeypatch) -> None:
    """``KNOWLEDGE_SQL_LOG`` controls statement logging on its own."""
    monkeypatch.setenv("KNOWLEDGE_SQL_LOG", "false")
    assert logging_setup.sql_logging_enabled() is False
    for value in ("true", "TRUE", "1", "yes", "on"):
        monkeypatch.setenv("KNOWLEDGE_SQL_LOG", value)
        assert logging_setup.sql_logging_enabled() is True
    monkeypatch.setenv("KNOWLEDGE_SQL_LOG", "0")
    assert logging_setup.sql_logging_enabled() is False


def test_sql_logger_is_enabled_by_default(log_env) -> None:
    """Statement visibility was the explicit request, so it defaults on."""
    handler = logging_setup.configure_logging("knowledge-api")
    try:
        assert logging.getLogger("knowledge-sql").isEnabledFor(logging.INFO)
    finally:
        if handler:
            handler.close()


def test_sql_logger_is_quiet_when_disabled(log_env, monkeypatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_SQL_LOG", "false")
    handler = logging_setup.configure_logging("knowledge-api")
    try:
        assert not logging.getLogger("knowledge-sql").isEnabledFor(logging.INFO)
    finally:
        if handler:
            handler.close()


# ----------------------------------------------------- source / compose guards


def test_store_routes_both_connection_paths_through_the_logging_class() -> None:
    """Pooled *and* direct connections must log their statements."""
    source = (KNOWLEDGE_DIR / "app" / "store.py").read_text(encoding="utf-8")
    assert "class LoggingConnection(psycopg.Connection)" in source
    assert "connection_class=LoggingConnection" in source
    assert "LoggingConnection.connect(" in source
    # The un-instrumented constructor must not remain on the fallback path.
    assert "psycopg.connect(self.database_url" not in source


def test_main_and_worker_share_the_logging_setup() -> None:
    """Both entry points must configure file logging, not just basicConfig."""
    for name in ("main.py", "worker.py"):
        source = (KNOWLEDGE_DIR / "app" / name).read_text(encoding="utf-8")
        assert "configure_logging(" in source, name
        assert "basicConfig(" not in source, name


@pytest.mark.parametrize(
    "compose_name",
    ["docker-compose.code-mount.arm64.yml", "docker-compose.code-mount.amd64.yml"],
)
def test_compose_mounts_logs_for_api_worker_and_lightrag(compose_name) -> None:
    """All three non-Postgres services need a host-visible log directory."""
    text = (KNOWLEDGE_DIR / compose_name).read_text(encoding="utf-8")
    # One mount per service: api, worker, lightrag.
    assert text.count(":/var/log/knowledge") == 3, "expected three log mounts"
    assert text.count("KNOWLEDGE_LOG_DIR: /var/log/knowledge") == 2, "api + worker"
    # LightRAG writes lightrag.log through its own LOG_DIR handling.
    assert "LOG_DIR: /var/log/knowledge" in text
    assert "KNOWLEDGE_HOST_LOG_DIR" in text


def test_log_mount_is_separate_from_the_data_mount() -> None:
    """Logs must not live inside /data, which is replaced on redeploy."""
    text = (KNOWLEDGE_DIR / "docker-compose.code-mount.arm64.yml").read_text(
        encoding="utf-8",
    )
    assert "./data/logs" in text
    assert "/data/logs" not in text.replace("./data/logs", "")
