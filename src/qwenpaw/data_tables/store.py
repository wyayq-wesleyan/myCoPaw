"""Data tables module — metadata storage in system PG17."""

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
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


def cleanup_staged_file(file_path: str | os.PathLike) -> None:
    """Remove a staged import file and its now-empty per-job directory.

    Every upload is staged as ``<UPLOAD_DIR>/<account>/<task_id>/<filename>``, so
    the job owns that directory outright. Only the file used to be deleted,
    which left one empty directory behind per import and made the staging tree
    grow with every upload. The directory is removed with ``rmdir`` and only
    after the file is gone, so a directory that still holds something — a
    retry's copy, for instance — is left alone.
    """
    path = Path(file_path)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning(
            "Could not remove staged import file %s", path, exc_info=True,
        )
        return

    try:
        root = Path(UPLOAD_DIR).resolve()
        task_dir = path.parent.resolve()
    except OSError:
        return
    # Expect exactly <root>/<account>/<task_id>. The two-level check means this
    # can never walk up into an account directory or the uploads root itself.
    if task_dir.parent.parent != root:
        return
    try:
        task_dir.rmdir()  # succeeds only while the directory is empty
    except OSError:
        pass


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
    """Create metadata tables in system PG17 if not exist.

    Also recovers rows orphaned by the pre-``tb_hzxzh_`` table rename; that step
    is idempotent, so it costs nothing once the metadata is in place.
    """
    global _schema_initialized
    if _schema_initialized:
        return
    with _schema_lock:
        if _schema_initialized:
            return
        _init_data_tables_schema()
        migrate_legacy_registry_tables()
        sync_registry_path_mirror()
        # Bindings left behind by deletes that predate the release-on-delete fix.
        # They inflate the path tree's counts and block deleting the path they
        # point at, so they are cleared once per process at start-up.
        prune_orphan_table_bindings()
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
                # Path scoping: which path a table belongs to. ``''`` means unbound,
                # which reads as "in the account's root path" (its public area) —
                # see platform_store.paths.effective_path_of. Existing tables keep
                # working unchanged, and isolating one is a single column update
                # plus a schema move.
                f"ALTER TABLE {SYSTEM_TABLE_REGISTRY} ADD COLUMN IF NOT EXISTS path_id TEXT NOT NULL DEFAULT ''",
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


# --- path schemas ---------------------------------------------------------
#
# Path isolation for data tables is *physical*: each path's tables live in their
# own PostgreSQL schema, and a connection is pinned to one schema's
# ``search_path`` (see ``connection.get_target_connection``). An unqualified table
# name therefore resolves only within the path's schema — a query cannot reach
# another case's table even by name.
#
# The account's **root** path maps to the ``public`` schema, which is where every
# existing table already lives. That choice makes the upgrade free: pre-existing
# tables *are* the root/public area, so nothing has to be moved, and binding a
# table to a child path is a schema move (one ``ALTER TABLE ... SET SCHEMA``)
# rather than a data copy.

# Must match ``agents.tools.path_scope.ROOT_SCHEMA``; asserted by a test.
ROOT_SCHEMA = "public"


class TableNotFound(LookupError):
    """No registry row for the requested table id (a client-side mistake).

    Distinct from a generic ``ValueError`` so the router can answer 404 rather than
    500 — and so a malformed id (``id::text`` avoids the uuid cast error) reads as
    "不存在" instead of a raw database message.
    """


def schema_for_path(path_id: str, is_root: bool) -> str:
    """The schema holding one path's data tables.

    Root → ``public`` (existing tables stay put). Any other path → a
    UUID-derived name from :func:`platform_store.paths.data_schema_for`.
    """
    if is_root:
        return ROOT_SCHEMA
    from ..platform_store import paths

    return paths.data_schema_for(path_id)


def schema_for_registry_row(row) -> str:
    """The schema a registry row's physical table belongs in.

    **Membership has exactly one source of truth: ``platform.resource_bindings``.**
    An earlier revision read a ``path_id`` column on the registry table instead,
    while the listings read the binding table — so filing a table into a case made
    it appear under that case (binding) while its data stayed in ``public``
    (column). The case's agent then could not reach the table it was shown, which
    is precisely the isolation failure this feature exists to prevent.

    Unbound means the account's public area, which is ``public`` (the root path) —
    that is why the upgrade moves nothing and why pre-existing tables keep working.
    A path id that no longer exists also resolves to the root rather than raising:
    a deleted path must not make a stored table unreadable.

    Accepts a mapping or ``psycopg2`` row; needs ``id`` to look up the binding.
    """
    try:
        table_id = str(row["id"])
    except (KeyError, TypeError, IndexError):
        return ROOT_SCHEMA
    try:
        from ..platform_store import paths

        path_id = paths.path_of_resource(paths.KIND_DATA_TABLE, table_id)
        if not path_id:
            return ROOT_SCHEMA
        node = paths.path_by_id(path_id)
    except Exception:  # noqa: BLE001 - unresolvable membership falls back to root
        logger.warning(
            "data tables: could not resolve the path for table %s; using the "
            "root schema",
            table_id, exc_info=True,
        )
        return ROOT_SCHEMA
    if not node:
        return ROOT_SCHEMA
    return schema_for_path(path_id, bool(node.get("is_root")))


def ensure_path_schema(db_name: str, schema: str) -> None:
    """Create a path's schema in one account database if it is missing.

    Uses a short-lived connection (not the pool) because the pool is keyed by
    schema: creating a schema through a connection pinned to a not-yet-existing
    schema would fail.
    """
    if schema == ROOT_SCHEMA:
        return
    from .connection import _system_db_config

    cfg = _system_db_config()
    conn = psycopg2.connect(
        host=cfg["host"],
        port=cfg["port"],
        dbname=db_name,
        user=cfg["username"],
        password=cfg["password"],
        connect_timeout=10,
    )
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            # ``schema`` is derived from a UUID (see data_schema_for) and only ever
            # contains [a-z0-9_], so it cannot break out of the quotes.
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    finally:
        conn.close()


def move_table_to_schema(
    db_name: str,
    table_name: str,
    from_schema: str,
    to_schema: str,
) -> None:
    """Move one table between path schemas (``ALTER TABLE ... SET SCHEMA``).

    A metadata-only operation in PostgreSQL: the table's data stays where it is
    on disk, so moving even a large table is instant. That is what makes "把公共
    路径下的表移到新路径" cheap.
    """
    if from_schema == to_schema:
        return
    ensure_path_schema(db_name, to_schema)
    from .connection import _system_db_config

    cfg = _system_db_config()
    conn = psycopg2.connect(
        host=cfg["host"],
        port=cfg["port"],
        dbname=db_name,
        user=cfg["username"],
        password=cfg["password"],
        connect_timeout=10,
    )
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                f'ALTER TABLE "{from_schema}"."{table_name}" '
                f'SET SCHEMA "{to_schema}"',
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_tables_in_schema(db_name: str, schema: str) -> list[str]:
    """User tables present in one path schema."""
    from .connection import _system_db_config

    cfg = _system_db_config()
    conn = psycopg2.connect(
        host=cfg["host"],
        port=cfg["port"],
        dbname=db_name,
        user=cfg["username"],
        password=cfg["password"],
        connect_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s "
                "ORDER BY tablename",
                (schema,),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


# Legacy table names, from before the ``tb_hzxzh_`` prefix was introduced. The
# rename shipped without a data migration, so on any deployment that had already
# imported data the old rows were orphaned: the new (empty) tables are what the
# code reads, so the data-analysis listing showed nothing even though the tables
# themselves were still in the account databases.
#
# Copying is the fix, and it is deliberately conservative:
#   * only rows missing from the new table are copied (matched by ``id``), so a
#     deployment that registered some tables before the rename and some after
#     ends up with both sets rather than one overwriting the other;
#   * the old table is left in place — until the copy is verified it is the only
#     remaining copy of that metadata;
#   * a missing old table is not an error (fresh deployments never had one).
_LEGACY_TABLE_ALIASES: tuple[tuple[str, str], ...] = (
    ("data_table_registry", SYSTEM_TABLE_REGISTRY),
    ("data_import_job", SYSTEM_TABLE_IMPORT_JOB),
    ("data_source_config", SYSTEM_TABLE_SOURCE_CONFIG),
)


def migrate_legacy_registry_tables() -> dict[str, int]:
    """Copy rows out of the pre-rename tables. Returns ``{table: rows_copied}``.

    Idempotent: re-running copies nothing once the rows are present. Failures are
    logged and skipped per table — this runs on the schema-init path, so it must
    not be able to prevent the data-analysis feature from starting.
    """
    copied: dict[str, int] = {}
    conn = _get_conn()
    try:
        for old_name, new_name in _LEGACY_TABLE_ALIASES:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT to_regclass(%s) IS NOT NULL",
                        (f"public.{old_name}",),
                    )
                    if not cur.fetchone()[0]:
                        continue  # no legacy table on this deployment
                    # Only the columns both tables share, so a schema that gained
                    # columns (e.g. ``path_id``) does not break the copy.
                    cur.execute(
                        """
                        SELECT string_agg(quote_ident(c.column_name), ', '
                                          ORDER BY c.ordinal_position)
                        FROM information_schema.columns c
                        WHERE c.table_schema = 'public' AND c.table_name = %s
                          AND EXISTS (
                            SELECT 1 FROM information_schema.columns n
                            WHERE n.table_schema = 'public' AND n.table_name = %s
                              AND n.column_name = c.column_name
                          )
                        """,
                        (old_name, new_name),
                    )
                    columns = cur.fetchone()[0]
                    if not columns:
                        continue
                    cur.execute(
                        f'INSERT INTO {new_name} ({columns}) '  # noqa: S608 - names are literals above
                        f'SELECT {columns} FROM {old_name} '
                        f"ON CONFLICT (id) DO NOTHING",
                    )
                    moved = cur.rowcount
                conn.commit()
                if moved:
                    copied[old_name] = int(moved)
                    logger.info(
                        "data tables: recovered %d row(s) from legacy table %s",
                        moved, old_name,
                    )
            except Exception as exc:  # noqa: BLE001 - isolate one table
                conn.rollback()
                logger.warning(
                    "data tables: could not recover legacy table %s: %s",
                    old_name, exc,
                )
    finally:
        _put_conn(conn)
    return copied


def table_actual_schema(db_name: str, table_name: str) -> str | None:
    """Which schema *table_name* physically lives in, or ``None`` if absent.

    Needed because the physical location is the thing that actually enforces
    isolation, and it can disagree with the binding: a table filed before the
    move was wired up still sits in ``public``. Moving from the *actual* schema
    (rather than from the schema the binding implies) is what lets that backlog
    heal on the next move instead of failing with "relation does not exist".

    Only the account's own schemas are considered — ``public`` plus the
    UUID-derived ``p_*`` ones — so a same-named table in an unrelated schema is
    never picked up.
    """
    from ..platform_store import paths as path_store
    from .connection import _system_db_config

    cfg = _system_db_config()
    conn = psycopg2.connect(
        host=cfg["host"],
        port=cfg["port"],
        dbname=db_name,
        user=cfg["username"],
        password=cfg["password"],
        connect_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT schemaname FROM pg_tables WHERE tablename = %s "
                "AND (schemaname = %s OR schemaname LIKE %s) "
                "ORDER BY (schemaname = %s) DESC LIMIT 1",
                (
                    table_name, ROOT_SCHEMA,
                    f"{path_store.DATA_SCHEMA_PREFIX}%", ROOT_SCHEMA,
                ),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def move_table_for_path(table_id: str, target_path_id: str | None) -> dict[str, str]:
    """Move one table's physical storage to match its path binding.

    **Why this exists.** ``platform.resource_bindings`` records *membership*, but
    membership alone does not isolate anything: the agent-side search path is
    derived from the granted paths, so a table that is bound to a case while
    still physically sitting in ``public`` is (a) unreachable for the case's own
    agent and (b) still reachable for every agent that holds the root. Both are
    the opposite of the intent. The binding only becomes real isolation once the
    table lives in that path's schema, which is this call.

    ``target_path_id`` of ``None`` (or the root path) means the public area, i.e.
    ``public``. The move is a metadata-only ``ALTER TABLE ... SET SCHEMA``, so it
    is cheap even for a very large table.

    Returns the ``from``/``to`` schemas for logging and tests. Raises
    :class:`TableNotFound` when the id is not one of this registry's rows, so the
    caller can answer "no such table" instead of leaking a database error.
    """
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            # ``id::text`` rather than ``id = %s``: the column is ``uuid`` and a
            # non-UUID string would make PostgreSQL reject the *type* ("invalid
            # input syntax for type uuid") before it can answer "no such row" —
            # surfacing a raw DB error for what is really a bad request.
            cur.execute(
                f"SELECT db_name, table_name FROM {SYSTEM_TABLE_REGISTRY} "
                "WHERE id::text = %s",
                (str(table_id),),
            )
            row = cur.fetchone()
    finally:
        _put_conn(conn)
    if not row:
        raise TableNotFound(f"数据表不存在：{table_id}")
    db_name, table_name = row[0], row[1]

    to_schema = ROOT_SCHEMA
    if target_path_id:
        from ..platform_store import paths as path_store

        node = path_store.path_by_id(target_path_id)
        if node and not node.get("is_root"):
            to_schema = path_store.data_schema_for(target_path_id)

    from_schema = table_actual_schema(db_name, table_name)
    if from_schema is None:
        # Never initialized (no physical table yet): there is nothing to move, and
        # the importer will create the table in the target schema on first import.
        #
        # The mirror is updated here too. Skipping it (as an earlier revision did)
        # left the raw registry row naming the *old* path while the binding named
        # the new one — exactly the "raw row says the opposite of the binding"
        # confusion the mirror column exists to prevent. The fix surfaced in
        # practice: filing a not-yet-imported table into a case looked like it had
        # silently failed, even though the binding was correct.
        _write_registry_path_mirror(table_id, target_path_id)
        logger.info(
            "data tables: %s has no physical table yet; binding recorded only "
            "(target %s)",
            table_name, to_schema,
        )
        return {"from": "", "to": to_schema, "moved": "false"}

    move_table_to_schema(db_name, table_name, from_schema, to_schema)

    # Keep the readable mirror column in step, so anyone reading the raw registry
    # row does not draw the opposite conclusion to the binding.
    _write_registry_path_mirror(table_id, target_path_id)

    logger.info(
        "data tables: moved %s.%s from %s to %s",
        db_name, table_name, from_schema, to_schema,
    )
    return {"from": from_schema, "to": to_schema, "moved": "true"}


def _write_registry_path_mirror(table_id: str, target_path_id: str | None) -> None:
    """Sync the registry's readable ``path_id`` column to the authoritative binding.

    Best-effort by design: the binding is the source of truth, so a failure here
    leaves the mirror stale (healed by ``sync_registry_path_mirror``) rather than
    failing the caller's move.
    """
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {SYSTEM_TABLE_REGISTRY} SET path_id=%s WHERE id=%s",
                (str(target_path_id or ""), table_id),
            )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - the move already happened
        conn.rollback()
        logger.warning("data tables: could not update path mirror: %s", exc)
    finally:
        _put_conn(conn)


def drop_path_schema(owner_username: str, path_id: str) -> list[str]:
    """Drop a deleted path's schema in the account database, if it is empty.

    Only empty schemas are dropped: a path can only be deleted once nothing is
    filed in it, so anything left here is a leftover rather than live data, and
    ``CASCADE`` is never used — if a table somehow survived, dropping the schema
    would silently destroy it. Returns the schemas actually dropped.
    """
    from ..platform_store import paths as path_store

    schema = path_store.data_schema_for(path_id)
    db_name = owner_username
    from .connection import _system_db_config

    cfg = _system_db_config()
    try:
        conn = psycopg2.connect(
            host=cfg["host"],
            port=cfg["port"],
            dbname=db_name,
            user=cfg["username"],
            password=cfg["password"],
            connect_timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - the account may have no database yet
        logger.info("data tables: no database for %s (%s)", owner_username, exc)
        return []
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_tables WHERE schemaname = %s", (schema,)
            )
            remaining = cur.fetchone()[0]
            if remaining:
                logger.warning(
                    "data tables: not dropping schema %s — it still holds %d table(s)",
                    schema, remaining,
                )
                conn.rollback()
                return []
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}"')
        conn.commit()
        logger.info("data tables: dropped schema %s in %s", schema, db_name)
        return [schema]
    except Exception:  # noqa: BLE001 - a leftover schema is not worth failing the delete
        conn.rollback()
        logger.warning("data tables: could not drop schema %s", schema, exc_info=True)
        return []
    finally:
        conn.close()


def sync_registry_path_mirror() -> dict[str, int]:
    """Align the registry's ``path_id`` column with the authoritative bindings.

    The column is a mirror kept for readability; membership lives in
    ``platform.resource_bindings``. A deployment that filed tables during the
    window when the two disagreed would otherwise keep showing stale values, and
    anyone reading the column directly would draw the wrong conclusion.

    Bindings win: the mirror is set from them. Idempotent, and failures are
    logged rather than raised (this runs on the schema-init path).
    """
    try:
        from ..platform_store import paths as path_store
    except Exception:  # noqa: BLE001 - nothing to sync without the store
        return {}
    updated = 0
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, owner_username, path_id FROM {SYSTEM_TABLE_REGISTRY}",
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("data tables: could not read the registry: %s", exc)
        _put_conn(conn)
        return {}
    finally:
        try:
            _put_conn(conn)
        except Exception:  # noqa: BLE001 - already returned
            pass

    for row_id, _owner, mirrored in rows:
        try:
            bound = path_store.path_of_resource(
                path_store.KIND_DATA_TABLE, str(row_id),
            ) or ""
        except Exception:  # noqa: BLE001 - skip rows we cannot resolve
            continue
        if bound == (mirrored or ""):
            continue
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {SYSTEM_TABLE_REGISTRY} SET path_id=%s WHERE id=%s",
                    (bound, row_id),
                )
            conn.commit()
            updated += 1
        except Exception as exc:  # noqa: BLE001 - isolate one row
            conn.rollback()
            logger.warning("data tables: could not sync path mirror: %s", exc)
        finally:
            _put_conn(conn)
    if updated:
        logger.info("data tables: aligned %d path mirror(s) with bindings", updated)
    return {"updated": updated}


def prune_orphan_table_bindings() -> dict[str, int]:
    """Delete bindings whose table no longer exists in this deployment's registry.

    A binding row is the authority for "which path holds this table", but it is not
    foreign-keyed to the registry (the resource may live in another store, as
    knowledge documents do). Deleting a table used to leave its binding behind, and
    those orphans were not harmless:

    * the path tree counted them, so a case showed more tables than its list held
      (the reported "路径数下面的个数和实际对不上");
    * ``path_is_empty`` saw them, so the case could never be deleted — and with no
      table visible to move out, the operator had no way to clear it.

    Deletes now release their binding, so this only has to clean deployments that
    predate that fix. Idempotent, and deliberately scoped to this account database's
    registry: knowledge documents are handled by their own store's lifecycle.

    Returns ``{"removed": n}``; failures are logged, not raised (this runs on the
    schema-init path, where a cleanup problem must not block start-up).
    """
    try:
        from ..platform_store import paths as path_store
    except Exception:  # noqa: BLE001 - nothing to prune without the store
        return {"removed": 0}

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT id FROM {SYSTEM_TABLE_REGISTRY}")
            live = {str(row[0]) for row in cur.fetchall()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("data tables: could not read the registry: %s", exc)
        return {"removed": 0}
    finally:
        _put_conn(conn)

    try:
        bound = path_store.any_bound_resource_ids(path_store.KIND_DATA_TABLE)
    except Exception as exc:  # noqa: BLE001
        logger.warning("data tables: could not read bindings: %s", exc)
        return {"removed": 0}

    orphans = sorted(bound - live)
    removed = 0
    for resource_id in orphans:
        try:
            path_store.unbind_resource(path_store.KIND_DATA_TABLE, resource_id)
            removed += 1
        except Exception:  # noqa: BLE001 - one bad row must not stop the rest
            logger.warning(
                "data tables: could not drop the orphan binding for %s",
                resource_id, exc_info=True,
            )
    if removed:
        logger.info(
            "data tables: removed %d orphan table binding(s) left by earlier "
            "deletes",
            removed,
        )
    return {"removed": removed}
