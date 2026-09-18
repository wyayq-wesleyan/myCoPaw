"""Per-account PostgreSQL database connection manager."""

import re
import time
import logging
import os
import uuid
import hashlib

import psycopg2
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger(__name__)

_target_pool: dict[str, ThreadedConnectionPool] = {}


def _slug_username(username: str) -> str:
    slug = re.sub(r"[^a-z0-9_]", "_", username.lower().strip())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug[:63] or "db_default"


def _database_name_for_username(username: str) -> str:
    """Return a PostgreSQL-safe, collision-resistant database name.

    A normal lowercase ASCII username remains unchanged, which keeps the
    database name intuitive. Names requiring normalization receive a stable
    hash suffix so different usernames can never collapse into one database.
    """
    raw = username.strip()
    if re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", raw):
        return raw
    slug = _slug_username(raw)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{slug[:52]}_{digest}"[:63]


def generate_table_name(label: str | None, description: str | None = None) -> str:
    """Generate a unique data-table name using the required prefix.

    Falls back to description, then to a timestamp-based name.
    """
    base = (label or description or "").strip()
    slug = re.sub(r"[^a-z0-9]", "", base.lower())
    if not slug:
        slug = f"data{int(time.time()) % 1000000}"
    slug = slug[:40]
    return f"tb_hzxzs_{slug}_{uuid.uuid4().hex[:8]}"[:63]


def sanitize_column_name(header: str, index: int) -> str:
    """Sanitize a CSV/Excel header into a valid PG column name.

    Falls back to col_<n> if the header is empty or yields no valid characters.
    """
    slug = re.sub(r"[^a-z0-9_]", "_", header.strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    if not slug or slug.isdigit():
        return f"col_{index + 1}"
    return slug[:63]


def _system_db_config() -> dict:
    """Return the PG17 connection used by assistant metadata and user databases.

    The data-source settings table is deliberately not consulted here.  That
    table may contain a historic external/HighGo connection, while account
    isolation requires all account databases to be created in the managed
    PG17 instance configured for this deployment.
    """
    configured_host = os.getenv("QWENPAW_DATA_TABLES_DB_HOST", "").strip()
    return {
        # The production assistant runs with host networking, so localhost is
        # both simpler and more reliable than Docker's host-gateway alias.
        "host": configured_host or "127.0.0.1",
        "port": int(os.getenv("QWENPAW_DATA_TABLES_DB_PORT", "15432")),
        "username": os.getenv("QWENPAW_DATA_TABLES_DB_USER", "huizhi_knowledge"),
        "password": os.getenv("QWENPAW_DATA_TABLES_DB_PASSWORD", "huizhi_knowledge"),
    }


def get_target_connection(db_name: str):
    if db_name in _target_pool:
        pool = _target_pool[db_name]
    else:
        cfg = _system_db_config()
        pool = ThreadedConnectionPool(
            1,
            5,
            host=cfg["host"],
            port=cfg["port"],
            dbname=db_name,
            user=cfg["username"],
            password=cfg["password"],
            connect_timeout=10,
        )
        _target_pool[db_name] = pool
    return pool.getconn()


def put_target_connection(conn, db_name: str):
    if db_name in _target_pool:
        _target_pool[db_name].putconn(conn)


def ensure_user_database(username: str) -> str:
    """Create and register one isolated database for an account.

    The database is created on the configured PostgreSQL server, while the
    mapping is stored in the assistant system database. Repeated calls are
    idempotent and return the same database name.
    """
    from .store import SYSTEM_TABLE_USER_DATABASE, init_data_tables_schema, _get_conn, _put_conn

    username = username.strip()
    if not username:
        raise ValueError("username is required")
    init_data_tables_schema()
    cfg = _system_db_config()
    db_name = _database_name_for_username(username)
    metadata_conn = _get_conn()
    try:
        with metadata_conn.cursor() as cur:
            cur.execute(
                f"SELECT database_name FROM {SYSTEM_TABLE_USER_DATABASE} WHERE username = %s",
                (username,),
            )
            existing = cur.fetchone()
            if existing:
                db_name = str(existing[0])
    finally:
        _put_conn(metadata_conn)

    admin_conn = psycopg2.connect(
        host=cfg["host"],
        port=cfg["port"],
        dbname="postgres",
        user=cfg["username"],
        password=cfg["password"],
        connect_timeout=10,
    )
    try:
        admin_conn.autocommit = True
        with admin_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{db_name}"')
                logger.info("Created data-analysis database for user '%s': %s", username, db_name)
    finally:
        admin_conn.close()

    metadata_conn = _get_conn()
    try:
        with metadata_conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {SYSTEM_TABLE_USER_DATABASE}
                    (username, database_name, status, error, updated_at)
                VALUES (%s, %s, 'ready', NULL, now())
                ON CONFLICT (username) DO UPDATE SET
                    database_name = EXCLUDED.database_name,
                    status = 'ready', error = NULL, updated_at = now()
                """,
                (username, db_name),
            )
            metadata_conn.commit()
    except Exception:
        metadata_conn.rollback()
        raise
    finally:
        _put_conn(metadata_conn)
    return db_name


def init_user_tables(db_name: str, table_name: str, columns: list[dict]) -> None:
    conn = get_target_connection(db_name)
    try:
        with conn.cursor() as cur:
            col_defs = []
            pk_cols = [c["name"] for c in columns if c.get("is_primary_key")]
            for col in columns:
                parts = [f'"{col["name"]}" {col["type"]}']
                if not col.get("nullable", True):
                    parts.append("NOT NULL")
                col_defs.append(" ".join(parts))
            if pk_cols:
                col_defs.append(f'PRIMARY KEY ({", ".join(f"{c}" for c in pk_cols)})')
            ddl = f'CREATE TABLE "{table_name}" ({", ".join(col_defs)})'
            cur.execute(ddl)
            for col in columns:
                if col.get("comment"):
                    cur.execute(
                        f'COMMENT ON COLUMN "{table_name}"."{col["name"]}" IS %s',
                        (col["comment"],),
                    )
            cur.execute(f'COMMENT ON TABLE "{table_name}" IS %s', (table_name,))
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_target_connection(conn, db_name)


def drop_table(db_name: str, table_name: str) -> None:
    conn = get_target_connection(db_name)
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{table_name}"')
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_target_connection(conn, db_name)


def truncate_table(db_name: str, table_name: str) -> None:
    conn = get_target_connection(db_name)
    try:
        with conn.cursor() as cur:
            cur.execute(f'TRUNCATE TABLE "{table_name}"')
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_target_connection(conn, db_name)
