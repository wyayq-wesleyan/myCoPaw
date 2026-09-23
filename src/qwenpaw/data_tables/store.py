"""Data tables module — metadata storage in system PG17."""

import json
import logging
import os
import threading
import time
import uuid
from typing import Any

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from ..config.utils import load_config
from ..constant import WORKING_DIR

logger = logging.getLogger(__name__)

_pool: ThreadedConnectionPool | None = None
# How long a caller waits for a metadata connection before giving up.
_POOL_WAIT_SECONDS = 10.0
_schema_lock = threading.Lock()
_schema_initialized = False

# Shared metadata tables are part of the assistant system database. Keep the
# names explicit and centralized so migrations and every API use one contract.
SYSTEM_TABLE_SOURCE_CONFIG = "tb_hzxzh_data_source_config"
SYSTEM_TABLE_REGISTRY = "tb_hzxzh_data_table_registry"
SYSTEM_TABLE_IMPORT_JOB = "tb_hzxzh_data_import_job"
SYSTEM_TABLE_USER_DATABASE = "tb_hzxzh_user_database"

# Import staging and limits. They live here rather than in ``import_api`` so the
# agent tools can read them without importing the pandas-backed importer.
#
# Derived from ``WORKING_DIR`` like every other storage location. It used to be
# the literal "/app/working/data_uploads", which silently ignored a relocated
# ``QWENPAW_WORKING_DIR``: every other path followed the override, so uploads
# landed beside the container's root filesystem instead and the write failed
# with a read-only error wherever /app does not exist.
UPLOAD_DIR = str(WORKING_DIR / "data_uploads")
ALLOWED_EXTENSIONS = {".csv", ".xls", ".xlsx"}

# Column types a caller may pin when defining a table.
#
# Every column defaults to TEXT. Type inference used to run on the first import
# and was wrong in exactly the cases that matter: an 11-digit phone number or a
# 15-digit IMEI matches "^[+-]?\d+$" and was declared INTEGER, so every insert
# failed with "integer out of range" and the import reported 0 rows. Identifiers
# (phone, IMEI, IMSI, account numbers, postal codes) are identifiers, not
# numbers, and must not be narrowed to a machine integer that cannot hold them.
#
# ``AUTO`` restores the old inference behavior but is opt-in per column, never
# the default. These values are interpolated into DDL, so the map is the
# allowlist: an unrecognized key is rejected rather than passed through.
COLUMN_TYPE_TEXT = "TEXT"
COLUMN_TYPE_AUTO = "AUTO"
ALLOWED_COLUMN_TYPES: dict[str, str | None] = {
    COLUMN_TYPE_TEXT: "TEXT",
    COLUMN_TYPE_AUTO: None,  # resolved by inference at first import
    "INTEGER": "INTEGER",
    "BIGINT": "BIGINT",
    "NUMERIC": "NUMERIC(20,4)",
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP",
    "BOOLEAN": "BOOLEAN",
}
MAX_IMPORT_FILE_BYTES = 10 * 1024 * 1024 * 1024
# xlrd/pandas cannot stream legacy XLS files. Keep their limit materially
# below the general limit so one upload cannot require several GB of RAM.
MAX_XLS_FILE_BYTES = 512 * 1024 * 1024


def user_upload_dir(username: str) -> str:
    """Return this account's import staging directory.

    Staged uploads used to share one flat directory with only a random task id
    for separation, so any account could read another's file if it learned the
    path. Each account now gets its own subtree.
    """
    import hashlib

    name = (username or "").strip() or "admin"
    # Keep the directory name filesystem-safe for Chinese/CJK usernames.
    safe = name if name.isascii() and name.isalnum() else hashlib.sha256(
        name.encode("utf-8"),
    ).hexdigest()[:24]
    return os.path.join(UPLOAD_DIR, safe)


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is not None:
        return _pool

    configured_host = os.getenv("QWENPAW_DATA_TABLES_DB_HOST", "").strip()
    hosts = [configured_host] if configured_host else ["host.docker.internal", "127.0.0.1"]
    last_error: Exception | None = None
    for host in hosts:
        try:
            _pool = ThreadedConnectionPool(
                1,
                10,
                host=host,
                port=int(os.getenv("QWENPAW_DATA_TABLES_DB_PORT", "15432")),
                dbname=os.getenv("QWENPAW_DATA_TABLES_DB_NAME", "huizhi_knowledge"),
                user=os.getenv("QWENPAW_DATA_TABLES_DB_USER", "huizhi_knowledge"),
                password=os.getenv("QWENPAW_DATA_TABLES_DB_PASSWORD", "huizhi_knowledge"),
                connect_timeout=10,
            )
            return _pool
        except Exception as exc:
            last_error = exc
            _pool = None
    raise RuntimeError(f"无法连接数据分析 PostgreSQL（尝试地址: {', '.join(hosts)}）: {last_error}")


def _get_conn():
    """Check out a metadata connection, waiting briefly when the pool is full.

    ``ThreadedConnectionPool.getconn`` raises ``PoolError`` the instant every
    slot is taken, which turned a short burst of concurrent requests into a
    confusing 500 ("connection pool exhausted"). Waiting for a slot instead
    degrades that into ordinary latency. The wait is bounded so a genuinely
    stuck pool still fails rather than hanging a worker thread forever.
    """
    global _pool
    pool = _get_pool()
    deadline = time.monotonic() + _POOL_WAIT_SECONDS
    delay = 0.02
    while True:
        try:
            return pool.getconn()
        except psycopg2.pool.PoolError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)
            # A previous holder may have closed the pool on error; re-resolve so
            # the wait does not spin forever against a discarded pool object.
            if _pool is not pool:
                pool = _get_pool()


def _put_conn(conn):
    _pool.putconn(conn)


def init_data_tables_schema():
    """Create metadata tables in system PG17 if not exist."""
    global _schema_initialized
    if _schema_initialized:
        return
    with _schema_lock:
        if _schema_initialized:
            return
        _init_data_tables_schema()
        _schema_initialized = True


def _init_data_tables_schema():
    """Create metadata tables while holding the schema initialization lock."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {SYSTEM_TABLE_SOURCE_CONFIG} (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    name TEXT UNIQUE NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL DEFAULT 5432,
                    username TEXT NOT NULL,
                    password_encrypted TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    updated_at TIMESTAMPTZ DEFAULT now()
                );
            """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {SYSTEM_TABLE_REGISTRY} (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    owner_username TEXT NOT NULL,
                    db_name TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    description TEXT DEFAULT '',
                    columns JSONB NOT NULL DEFAULT '[]',
                    row_count BIGINT DEFAULT 0,
                    initialized BOOLEAN NOT NULL DEFAULT false,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    updated_at TIMESTAMPTZ DEFAULT now(),
                    UNIQUE(owner_username, table_name)
                );
            """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {SYSTEM_TABLE_IMPORT_JOB} (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    table_id UUID NOT NULL REFERENCES {SYSTEM_TABLE_REGISTRY}(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    total_rows BIGINT DEFAULT 0,
                    success_rows BIGINT DEFAULT 0,
                    failed_rows BIGINT DEFAULT 0,
                    errors JSONB DEFAULT '[]',
                    file_path TEXT,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    started_at TIMESTAMPTZ,
                    finished_at TIMESTAMPTZ
                    ,attempts INTEGER NOT NULL DEFAULT 0
                    ,next_retry_at TIMESTAMPTZ
                    ,log_excerpt TEXT
                );
            """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {SYSTEM_TABLE_USER_DATABASE} (
                    username TEXT PRIMARY KEY,
                    database_name TEXT UNIQUE NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ready',
                    error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )
            conn.commit()
            # Existing databases predate some columns. Each statement commits on
            # its own: a failure aborts the psycopg2 transaction, and running
            # them in one shared transaction used to discard every later
            # migration silently, leaving the runtime queries with columns the
            # metadata tables never grew.
            migrations = [
                f"ALTER TABLE {SYSTEM_TABLE_REGISTRY} ADD COLUMN IF NOT EXISTS initialized BOOLEAN NOT NULL DEFAULT false",
                f"ALTER TABLE {SYSTEM_TABLE_REGISTRY} ADD COLUMN IF NOT EXISTS display_name TEXT NOT NULL DEFAULT ''",
                f"ALTER TABLE {SYSTEM_TABLE_REGISTRY} ADD COLUMN IF NOT EXISTS db_name TEXT NOT NULL DEFAULT ''",
                f"ALTER TABLE {SYSTEM_TABLE_IMPORT_JOB} ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ",
                f"ALTER TABLE {SYSTEM_TABLE_IMPORT_JOB} ADD COLUMN IF NOT EXISTS file_path TEXT",
                f"ALTER TABLE {SYSTEM_TABLE_IMPORT_JOB} ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0",
                f"ALTER TABLE {SYSTEM_TABLE_IMPORT_JOB} ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ",
                f"ALTER TABLE {SYSTEM_TABLE_IMPORT_JOB} ADD COLUMN IF NOT EXISTS log_excerpt TEXT",
            ]
            for statement in migrations:
                try:
                    with conn.cursor() as cur:
                        cur.execute(statement)
                    conn.commit()
                except Exception as exc:
                    conn.rollback()
                    logger.warning("data tables migration skipped (%s): %s", exc, statement)
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)
