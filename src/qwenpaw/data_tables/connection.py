"""Per-account PostgreSQL database connection manager."""

import re
import time
import logging
import os
import threading
import uuid
import hashlib

import psycopg2
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger(__name__)

# Upper bound on simultaneously open per-account pools. With ``maxconn`` 2 this
# keeps the worst case at 2 * _MAX_LIVE_POOLS plus the metadata pool, well under
# PostgreSQL's default 100 connections even as accounts are added.
_MAX_LIVE_POOLS = 16

# Keyed by ``(db_name, schema)``: a connection is permanently bound to one path's
# search_path, so two paths can never share a pooled connection. ``schema`` is
# ``""`` for the unscoped (metadata / import) pools.
_target_pool: dict[tuple[str, str], ThreadedConnectionPool] = {}
# Access order per key, oldest first (LRU). Kept alongside the pool map so idle
# pools can be reclaimed instead of accumulating one per (account, path).
_target_pool_order: list[tuple[str, str]] = []

# Import jobs run in worker threads, so pool creation and LRU bookkeeping need
# a real thread lock. Without it two jobs for the same account both miss the
# lookup and build a pool, and the loser's connections are unreachable from then
# on (the map holds only one of them), which surfaces as "connection pool
# exhausted" once the surviving pool's two connections are in use.
_pool_lock = threading.RLock()


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


def _reclaim_idle_pools(keep: tuple[str, str]) -> None:
    """Close least-recently-used pools until at most ``_MAX_LIVE_POOLS`` remain.

    Every account gets its own database — and now every path within an account
    gets its own pool too (a connection is bound to one path's search_path), so
    the count grows with accounts x paths. Without a cap they are never released:
    30 accounts x maxconn 5 plus the metadata pool reaches ~160 connections, above
    PostgreSQL's default ``max_connections`` of 100, and the server starts
    refusing connections ("too many clients"). Pools are cheap to recreate (they
    re-pin their search_path on creation), so idle ones are closed rather than
    kept forever.

    Only pools with no checked-out connection are closed; a pool still serving a
    request is skipped, which keeps eviction safe while queries are in flight.
    """
    while len(_target_pool) > _MAX_LIVE_POOLS:
        victim = next(
            (
                name
                for name in _target_pool_order
                if name != keep and getattr(_target_pool[name], "_used", None) == {}
            ),
            None,
        )
        if victim is None:
            return
        pool = _target_pool.pop(victim)
        _target_pool_order.remove(victim)
        try:
            pool.closeall()
        except Exception:  # noqa: BLE001 - reclaiming must never break a query
            logger.warning(
                "Could not close idle data-table pool for %s", victim, exc_info=True,
            )


def close_all_target_pools() -> None:
    """Close every per-account pool (shutdown / tests)."""
    for pool in _target_pool.values():
        try:
            pool.closeall()
        except Exception:  # noqa: BLE001
            logger.warning("Could not close a data-table pool", exc_info=True)
    _target_pool.clear()
    _target_pool_order.clear()


def _normalize_search_path(schema) -> tuple[str, str]:
    """Return ``(pool_key_fragment, search_path_sql)`` for a schema or list.

    A list is joined into PostgreSQL's comma-separated ``search_path`` form, which
    is how one connection sees several paths at once (an agent's cases first, the
    shared ``public`` area last). An empty list is treated as *no* schema — the
    caller asked for a path set and got nothing, which must not silently become
    "unscoped access to everything".
    """
    if schema is None:
        return "", ""
    if isinstance(schema, str):
        return schema, schema
    names = [str(item) for item in schema if item]
    if not names:
        return "", ""
    # ``options="-c search_path=..."`` is parsed by PostgreSQL's startup-option
    # parser, where a space separates *options* — so a space inside the value
    # makes the list invalid ("invalid value for parameter search_path"). The
    # separator there must be a bare comma with no spaces. ``SET search_path``
    # (used on checkout) accepts either form, so the same string is reused.
    joined = ",".join(names)
    return joined, joined


def get_target_connection(db_name: str, schema=None):
    """Check out a connection to an account database.

    ``schema`` scopes the connection to one path's tables (path isolation): the
    connection's ``search_path`` is pinned to it, so an unqualified table name can
    only resolve within those paths. Accepts a single name or an ordered list (a
    list lets one agent see its case *and* the shared public area, with the case
    taking precedence). ``None`` keeps the default search path (legacy behaviour,
    which the metadata and import paths use).

    An empty list is refused by the caller's contract — see
    ``path_scope.data_search_path`` returning ``None`` to mean "deny".

    **The pool is keyed by ``(db_name, schema)``, not by database alone.** A
    connection's ``search_path`` is session state — if pools were shared across
    paths, a connection returned by one case could hand its schema to the next
    borrower. Keying the pool by schema makes that impossible by construction
    rather than by remembering to reset.
    """
    schema_key, search_path_sql = _normalize_search_path(schema)
    key = (db_name, schema_key)
    if schema is not None and not search_path_sql:
        raise ValueError("拒绝在无 schema 的连接上执行数据查询（fail-closed）")
    with _pool_lock:
        if key in _target_pool:
            pool = _target_pool[key]
            if key in _target_pool_order:
                _target_pool_order.remove(key)
            _target_pool_order.append(key)
        else:
            cfg = _system_db_config()
            connect_kwargs = {
                "host": cfg["host"],
                "port": cfg["port"],
                "dbname": db_name,
                "user": cfg["username"],
                "password": cfg["password"],
                "connect_timeout": 10,
            }
            if search_path_sql:
                # Set at connection start rather than afterwards: there is then no
                # window in which the wrong schema is active.
                connect_kwargs["options"] = f"-c search_path={search_path_sql}"
            pool = ThreadedConnectionPool(
                # minconn kept at 1 so a pool costs one connection while idle; the
                # ceiling is deliberately small because concurrency per account is
                # low (imports are globally limited to a couple at a time) and the
                # total across accounts must stay under the server's limit.
                1,
                2,
                **connect_kwargs,
            )
            _target_pool[key] = pool
            _target_pool_order.append(key)
            _reclaim_idle_pools(keep=key)
        conn = pool.getconn()
    if search_path_sql:
        _pin_search_path(conn, search_path_sql)
    return conn


def _pin_search_path(conn, schema: str) -> None:
    """Re-assert ``search_path`` on every checkout.

    A connection is long-lived, and ``SET`` is session state that survives a
    transaction: if any statement (or a function it calls, e.g. ``set_config``)
    changed ``search_path``, the next borrower of that connection would inherit
    it. Re-asserting on checkout costs a trivial round-trip and removes the need
    to trust that nothing did. Deliberately not ``SET LOCAL``: the setting must
    hold for the whole checkout, not just one transaction.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path = {schema}")
        conn.commit()
    except Exception:  # noqa: BLE001 - discard rather than hand out an unscoped conn
        logger.warning(
            "Could not pin search_path to %s; discarding the connection", schema,
        )
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        raise


def put_target_connection(conn, db_name: str, schema=None):
    """Return a connection. The key must match the one used to check it out.

    Defaulting ``schema`` to ``None`` keeps existing callers (metadata and import
    paths, which never scope to a schema) working unchanged, while a path-scoped
    caller passes the same schema it checked out with.
    """
    schema_key, _ = _normalize_search_path(schema)
    key = (db_name, schema_key)
    with _pool_lock:
        pool = _target_pool.get(key)
    if pool is not None:
        pool.putconn(conn)
    else:
        # No matching pool (e.g. it was reclaimed): close rather than leak.
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


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
