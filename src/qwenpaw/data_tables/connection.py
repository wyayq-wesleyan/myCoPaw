"""HighGo database connection manager."""

import re
import time
import logging

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger(__name__)

_target_pool: dict[str, ThreadedConnectionPool] = {}
_source_config: dict | None = None


def _slug_username(username: str) -> str:
    slug = re.sub(r"[^a-z0-9_]", "_", username.lower().strip())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug[:63] or "db_default"


def generate_table_name(label: str | None, description: str | None = None) -> str:
    """Generate a valid table name tb_<slug> from a user-provided label.

    Falls back to description, then to a timestamp-based name.
    """
    base = (label or description or "").strip()
    slug = re.sub(r"[^a-z0-9]", "", base.lower())
    if not slug:
        slug = f"data{int(time.time()) % 1000000}"
    slug = slug[:40]
    return f"tb_{slug}"


def sanitize_column_name(header: str, index: int) -> str:
    """Sanitize a CSV/Excel header into a valid PG column name.

    Falls back to col_<n> if the header is empty or yields no valid characters.
    """
    slug = re.sub(r"[^a-z0-9_]", "_", header.strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    if not slug or slug.isdigit():
        return f"col_{index + 1}"
    return slug[:63]


def _load_source_config():
    from .store import _get_conn, _put_conn

    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM data_source_config ORDER BY created_at DESC LIMIT 1")
            row = cur.fetchone()
            if not row:
                raise RuntimeError("数据源未配置，请在管理端设置数据源")
            return {
                "host": row["host"],
                "port": row["port"],
                "username": row["username"],
                "password": row["password_encrypted"],
            }
    finally:
        _put_conn(conn)


def _get_source_config() -> dict:
    global _source_config
    if _source_config is None:
        _source_config = _load_source_config()
    return _source_config


def get_target_connection(db_name: str):
    if db_name in _target_pool:
        pool = _target_pool[db_name]
    else:
        cfg = _get_source_config()
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
    db_name = _slug_username(username)
    cfg = _get_source_config()
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
                logger.info("Created database: %s", db_name)
    finally:
        admin_conn.close()
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
