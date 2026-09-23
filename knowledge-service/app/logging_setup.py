# -*- coding: utf-8 -*-
"""Shared logging setup for the knowledge API and worker.

Both processes log to stdout (so ``docker logs`` keeps working) **and** to a
rotating file under ``KNOWLEDGE_LOG_DIR``, which the Compose files bind-mount to
a host directory. That file is the part that matters in production: ``docker
logs`` is only reachable while the container still exists and its json-file log
is rotated away on recreation, so a crash or a redeploy used to leave nothing to
inspect.

Statement logging is configured here as well. ``psycopg`` 3 does **not** log the
SQL it runs at any level — it only logs connection attempts and transactions —
so ``store.py`` emits the statements itself through the ``knowledge-sql`` logger,
whose level is controlled independently by ``KNOWLEDGE_SQL_LOG``.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

# Container path; the Compose files mount the host directory here.
DEFAULT_LOG_DIR = "/var/log/knowledge"

# Mirror the main application's supervisor rotation (20MB x 5) so the host log
# directory cannot grow without bound.
MAX_BYTES = 20 * 1024 * 1024
BACKUP_COUNT = 5

_TRUTHY = {"1", "true", "yes", "on"}


def log_dir() -> Path:
    """Directory log files are written to."""
    return Path(os.environ.get("KNOWLEDGE_LOG_DIR", DEFAULT_LOG_DIR)).expanduser()


def sql_logging_enabled() -> bool:
    """Whether executed SQL statements are written to the log."""
    return os.environ.get("KNOWLEDGE_SQL_LOG", "true").strip().lower() in _TRUTHY


def _file_handler(service: str) -> logging.Handler | None:
    """Rotating file handler for one service, or ``None`` if unwritable."""
    directory = log_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        return logging.handlers.RotatingFileHandler(
            directory / f"{service}.log",
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError as exc:
        # A read-only or missing mount must not stop the service from starting:
        # it degrades to console-only logging, exactly like before this change.
        logging.getLogger(__name__).warning(
            "cannot write logs under %s (%s); continuing with console logging only",
            directory,
            exc,
        )
        return None


def configure_logging(service: str) -> logging.Handler | None:
    """Configure console + rotating-file logging for *service*.

    Returns the file handler so the caller can also attach it to the ``uvicorn``
    loggers once uvicorn has installed its own logging (it replaces handlers on
    startup, and those loggers do not propagate to the root logger).
    """
    level = getattr(
        logging, os.environ.get("KNOWLEDGE_LOG_LEVEL", "INFO").upper(), logging.INFO,
    )
    formatter = logging.Formatter(
        f"%(asctime)s %(levelname)s {service} %(message)s",
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    handlers: list[logging.Handler] = [console]

    file_handler = _file_handler(service)
    if file_handler is not None:
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    # force=True: this module owns the root configuration for these processes.
    logging.basicConfig(level=level, handlers=handlers, force=True)

    # Statement logging is a separate switch: an operator may want the service's
    # own logs without the (much larger) SQL stream, or the reverse.
    logging.getLogger("knowledge-sql").setLevel(
        logging.INFO if sql_logging_enabled() else logging.WARNING,
    )
    return file_handler


def attach_to_uvicorn(file_handler: logging.Handler | None) -> None:
    """Send uvicorn's own logs to the same file.

    ``uvicorn`` and ``uvicorn.access`` set ``propagate = False`` and install
    their own handlers when the server starts, so root-level handlers never see
    them. Called from the app's lifespan, which runs after that setup, this
    adds the file handler so request logs also land on the host.
    """
    if file_handler is None:
        return
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        if file_handler not in uvicorn_logger.handlers:
            uvicorn_logger.addHandler(file_handler)


__all__ = [
    "DEFAULT_LOG_DIR",
    "attach_to_uvicorn",
    "configure_logging",
    "log_dir",
    "sql_logging_enabled",
]
