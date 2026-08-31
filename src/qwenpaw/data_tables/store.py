"""Data tables module — metadata storage in system PG17."""

import json
import uuid
from typing import Any

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from ..config.utils import load_config

_pool: ThreadedConnectionPool | None = None


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is not None:
        return _pool

    # Connect to the knowledge-postgres container via host.docker.internal
    _pool = ThreadedConnectionPool(
        1,
        10,
        host="host.docker.internal",
        port=15432,
        dbname="huizhi_knowledge",
        user="huizhi_knowledge",
        password="huizhi_knowledge",
    )
    return _pool


def _get_conn():
    return _get_pool().getconn()


def _put_conn(conn):
    _pool.putconn(conn)


def init_data_tables_schema():
    """Create metadata tables in system PG17 if not exist."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS data_source_config (
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
                """
                CREATE TABLE IF NOT EXISTS data_table_registry (
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
                """
                CREATE TABLE IF NOT EXISTS data_import_job (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    table_id UUID NOT NULL REFERENCES data_table_registry(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    total_rows BIGINT DEFAULT 0,
                    success_rows BIGINT DEFAULT 0,
                    failed_rows BIGINT DEFAULT 0,
                    errors JSONB DEFAULT '[]',
                    file_path TEXT,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    finished_at TIMESTAMPTZ
                );
            """
            )
            conn.commit()
            # Migration: ensure initialized column exists on existing tables
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "ALTER TABLE data_table_registry ADD COLUMN IF NOT EXISTS initialized BOOLEAN NOT NULL DEFAULT false"
                    )
                    cur.execute(
                        "ALTER TABLE data_table_registry ADD COLUMN IF NOT EXISTS display_name TEXT NOT NULL DEFAULT ''"
                    )
                    conn.commit()
            except Exception:
                conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)
