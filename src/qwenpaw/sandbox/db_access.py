# -*- coding: utf-8 -*-
"""Per-account PostgreSQL access for the ordinary-user sandbox.

The sandbox has no TCP network, so user code cannot reach PostgreSQL directly.
This module provides two things:

1. A per-account PostgreSQL role with full DML/DDL rights **inside its own
   database only**.  ``PUBLIC`` CONNECT is revoked from every database; the
   role is granted CONNECT only on the account database.  It is not a member
   of the application's superuser role.
2. A UNIX-socket proxy that forwards PostgreSQL wire bytes to the local server.
   The proxy parses the startup packet and refuses any user/database other than
   the account's own role/database, so knowing the application's database
   password does not turn the proxy into a cross-tenant gateway.

The account side gets standard ``PG*`` environment variables pointing at the
UNIX socket; psycopg2 / psql / pandas work without any special client code.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import os
import secrets
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import psycopg2
from psycopg2 import sql

logger = logging.getLogger(__name__)

_SOCKET_PORT = 5432
_MAX_STARTUP_BYTES = 64 * 1024
_MAX_PROXY_CLIENTS = 16
_SOCKET_NAME = f".s.PGSQL.{_SOCKET_PORT}"

_PROVISION_LOCK = threading.Lock()
_PUBLIC_CONNECT_REVOKED = False
_PROXY_LOCK = asyncio.Lock()
_PROXIES: dict[str, "PostgresProxy"] = {}

_RESERVED_DATABASES = {
    "postgres",
    "template0",
    "template1",
    "huizhi_knowledge",
    "huizhi_knowledge_graph",
}


class DbAccessError(RuntimeError):
    """The account database/role could not be prepared safely."""


@dataclass(frozen=True)
class AccountDbAccess:
    username: str
    database: str
    role: str
    password: str
    socket_dir: Path
    socket_path: Path


def _app_db_config() -> dict:
    host = os.environ.get("QWENPAW_DATA_TABLES_DB_HOST", "").strip() or "127.0.0.1"
    return {
        "host": host,
        "port": int(os.environ.get("QWENPAW_DATA_TABLES_DB_PORT", "15432")),
        "user": os.environ.get("QWENPAW_DATA_TABLES_DB_USER", "huizhi_knowledge"),
        "password": os.environ.get(
            "QWENPAW_DATA_TABLES_DB_PASSWORD",
            "huizhi_knowledge",
        ),
        "dbname": os.environ.get(
            "QWENPAW_DATA_TABLES_DB_NAME",
            "huizhi_knowledge",
        ),
    }


def _connect(dbname: Optional[str] = None, autocommit: bool = True):
    cfg = _app_db_config()
    conn = psycopg2.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        dbname=dbname or cfg["dbname"],
        connect_timeout=10,
    )
    conn.autocommit = autocommit
    return conn


def _role_name(username: str) -> str:
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:20]
    return f"sbx_{digest}"


def _account_database(username: str) -> str:
    from ..data_tables.connection import _database_name_for_username

    name = ""
    # The registry is authoritative: a legacy/renamed account may have a
    # database name different from the current derivation.  Fall back to the
    # derivation only when the table or row is absent.
    try:
        from ..data_tables.store import SYSTEM_TABLE_USER_DATABASE

        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT database_name FROM {SYSTEM_TABLE_USER_DATABASE} "
                    "WHERE username = %s",
                    (username,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    name = str(row[0])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - metadata table may not exist yet
        logger.debug("sandbox db: registry lookup failed", exc_info=True)
    if not name:
        name = _database_name_for_username(username)
    # Two usernames must never share one account database: the sandbox role is
    # granted DML on every table in it.  Refuse a corrupted/duplicated mapping
    # rather than turning it into a cross-account read.
    try:
        from ..data_tables.store import SYSTEM_TABLE_USER_DATABASE

        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT username FROM {SYSTEM_TABLE_USER_DATABASE} "
                    "WHERE database_name = %s AND username <> %s LIMIT 1",
                    (name, username),
                )
                other = cur.fetchone()
                if other:
                    raise DbAccessError(
                        f"数据库 {name} 同时登记给账号 {other[0]!r} 和 "
                        f"{username!r}，拒绝授予沙箱数据库权限。",
                    )
        finally:
            conn.close()
    except DbAccessError:
        raise
    except Exception:  # noqa: BLE001 - table may be absent
        logger.debug("sandbox db: mapping collision check skipped", exc_info=True)
    if name in _RESERVED_DATABASES:
        raise DbAccessError(
            f"账号 {username!r} 的数据库名与系统库冲突（{name}），"
            "拒绝授予沙箱数据库权限。",
        )
    return name


def _secret_path(username: str) -> Path:
    from ..constant import SECRET_DIR

    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:20]
    return Path(SECRET_DIR) / "sandbox-db" / f"{digest}.json"


def _load_or_create_password(username: str) -> str:
    path = _secret_path(username)
    try:
        data = json.loads(path.read_text("utf-8"))
        password = str(data.get("password") or "")
        if password:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            return password
    except (OSError, json.JSONDecodeError):
        pass
    password = secrets.token_hex(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "username": username,
                "password": password,
            },
            ensure_ascii=False,
        ),
        "utf-8",
    )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return password


def _revoke_public_connect(conn) -> None:
    global _PUBLIC_CONNECT_REVOKED
    if _PUBLIC_CONNECT_REVOKED:
        return
    with conn.cursor() as cur:
        # Include template1: it accepts connections by default and a role
        # that can write there could seed objects into future databases.
        cur.execute("SELECT datname FROM pg_database")
        databases = [row[0] for row in cur.fetchall()]
        for name in databases:
            cur.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(name),
                ),
            )
            cur.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(name),
                    sql.Identifier(_app_db_config()["user"]),
                ),
            )
    _PUBLIC_CONNECT_REVOKED = True


def _grant_schema(cur, schema: str, role: str, app_user: str) -> None:
    ident_schema = sql.Identifier(schema)
    ident_role = sql.Identifier(role)
    ident_app = sql.Identifier(app_user)
    cur.execute(
        sql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO {}").format(
            ident_schema,
            ident_role,
        ),
    )
    cur.execute(
        sql.SQL(
            "GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, "
            "TRIGGER ON ALL TABLES IN SCHEMA {} TO {}",
        ).format(ident_schema, ident_role),
    )
    cur.execute(
        sql.SQL("GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA {} TO {}").format(
            ident_schema,
            ident_role,
        ),
    )
    cur.execute(
        sql.SQL("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA {} TO {}").format(
            ident_schema,
            ident_role,
        ),
    )
    cur.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} "
            "GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, "
            "TRIGGER ON TABLES TO {}",
        ).format(ident_app, ident_schema, ident_role),
    )
    cur.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} "
            "GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {}",
        ).format(ident_app, ident_schema, ident_role),
    )
    cur.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} "
            "GRANT EXECUTE ON FUNCTIONS TO {}",
        ).format(ident_app, ident_schema, ident_role),
    )


def _provision_sync(username: str) -> tuple[str, str, str]:
    """Create/refresh the account role and its privileges. Returns (db, role, pw)."""
    database = _account_database(username)
    role = _role_name(username)
    password = _load_or_create_password(username)
    app_user = _app_db_config()["user"]

    with _PROVISION_LOCK:
        admin = _connect(dbname="postgres")
        try:
            _revoke_public_connect(admin)
            with admin.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname = %s",
                    (role,),
                )
                exists = cur.fetchone() is not None
                if not exists:
                    cur.execute(
                        sql.SQL(
                            "CREATE ROLE {} LOGIN PASSWORD %s "
                            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT",
                        ).format(sql.Identifier(role)),
                        (password,),
                    )
                else:
                    cur.execute(
                        sql.SQL("ALTER ROLE {} PASSWORD %s").format(
                            sql.Identifier(role),
                        ),
                        (password,),
                    )
                cur.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s",
                    (database,),
                )
                if cur.fetchone() is None:
                    cur.execute(
                        sql.SQL("CREATE DATABASE {} OWNER {}").format(
                            sql.Identifier(database),
                            sql.Identifier(app_user),
                        ),
                    )
                    # The global revoke above already ran before this database
                    # existed; close its default PUBLIC CONNECT explicitly.
                    cur.execute(
                        sql.SQL(
                            "REVOKE CONNECT ON DATABASE {} FROM PUBLIC",
                        ).format(sql.Identifier(database)),
                    )
                    cur.execute(
                        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                            sql.Identifier(database),
                            sql.Identifier(app_user),
                        ),
                    )
        finally:
            admin.close()

        account = _connect(dbname=database)
        try:
            with account.cursor() as cur:
                cur.execute(
                    sql.SQL("GRANT CONNECT, TEMP ON DATABASE {} TO {}").format(
                        sql.Identifier(database),
                        sql.Identifier(role),
                    ),
                )
                cur.execute(
                    "SELECT nspname FROM pg_namespace "
                    "WHERE nspname NOT LIKE 'pg_%%' "
                    "AND nspname <> 'information_schema'",
                )
                schemas = [row[0] for row in cur.fetchall()]
                for schema in schemas:
                    _grant_schema(cur, schema, role, app_user)
        finally:
            account.close()

    return database, role, password


async def provision_account_db(username: str) -> tuple[str, str, str]:
    try:
        return await asyncio.to_thread(_provision_sync, username)
    except Exception as exc:  # noqa: BLE001 - translate to a domain refusal
        raise DbAccessError(f"无法准备账号数据库权限：{exc}") from exc


def _socket_dir_for(paths) -> Path:
    # Kept under outputs/ so Landlock's writable rule covers the socket and the
    # sandbox's per-account UID can connect.  The name is short enough for
    # AF_UNIX's 107-byte path limit.
    directory = paths.output_dir / "pgsock"
    if directory.is_symlink():
        directory.unlink()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(64 * 1024)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


class PostgresProxy:
    """UNIX-socket PostgreSQL proxy bound to one account's role/database."""

    def __init__(
        self,
        access: AccountDbAccess,
        host: str,
        port: int,
    ) -> None:
        self.access = access
        self.backend_host = host
        self.backend_port = port
        self._server: Optional[asyncio.AbstractServer] = None
        self._clients = 0

    async def start(self) -> None:
        socket_path = self.access.socket_path
        if socket_path.exists():
            try:
                socket_path.unlink()
            except OSError:
                pass
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(socket_path),
            backlog=32,
        )
        try:
            os.chown(socket_path, self.access_socket_uid, self.access_socket_gid)
        except OSError:
            logger.warning("sandbox db: could not chown socket", exc_info=True)
        try:
            os.chmod(socket_path, 0o660)
        except OSError as exc:
            if exc.errno == errno.EINVAL:
                # Docker Desktop bind mounts reject chmod on a socket; the
                # owning UID still has the needed permission.
                logger.debug("sandbox db: chmod socket unsupported", exc_info=True)
            else:
                logger.warning("sandbox db: could not chmod socket", exc_info=True)

    @property
    def access_socket_uid(self) -> int:
        return int(self.access.socket_dir.stat().st_uid)

    @property
    def access_socket_gid(self) -> int:
        return int(self.access.socket_dir.stat().st_gid)

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if self._clients >= _MAX_PROXY_CLIENTS:
            writer.close()
            return
        self._clients += 1
        try:
            rewritten = await self._read_checked_startup(reader, writer)
            if rewritten is None:
                return
            try:
                upstream_reader, upstream_writer = await asyncio.open_connection(
                    self.backend_host,
                    self.backend_port,
                )
            except OSError:
                return
            upstream_writer.write(rewritten)
            await upstream_writer.drain()
            await asyncio.gather(
                _pipe(reader, upstream_writer),
                _pipe(upstream_reader, writer),
            )
        finally:
            self._clients -= 1
            try:
                writer.close()
            except OSError:
                pass

    async def _read_checked_startup(self, reader, writer) -> Optional[bytes]:
        while True:
            try:
                header = await asyncio.wait_for(reader.readexactly(4), timeout=10)
                length = struct.unpack("!I", header)[0]
                if length < 8 or length > _MAX_STARTUP_BYTES:
                    return None
                body = await asyncio.wait_for(
                    reader.readexactly(length - 4),
                    timeout=10,
                )
            except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError):
                return None
            code = struct.unpack("!I", body[:4])[0]
            if code in (80877103, 80877104):
                writer.write(b"N")
                await writer.drain()
                continue
            if code != 196608:
                return None
            chunks = body[4:].split(b"\0")
            pairs: list[tuple[bytes, bytes]] = []
            i = 0
            while i + 1 < len(chunks) and chunks[i]:
                pairs.append((chunks[i], chunks[i + 1]))
                i += 2
            expected_user = self.access.role.encode()
            expected_db = self.access.database.encode()
            out: list[tuple[bytes, bytes]] = []
            seen_db = False
            for key, value in pairs:
                if key == b"user":
                    if value != expected_user:
                        return None
                elif key == b"database":
                    seen_db = True
                    if value != expected_db:
                        return None
                out.append((key, value))
            if not any(key == b"user" for key, _ in out):
                return None
            if not seen_db:
                out.append((b"database", expected_db))
            params = b"".join(key + b"\0" + value + b"\0" for key, value in out) + b"\0"
            return struct.pack("!II", len(params) + 8, 196608) + params

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


async def ensure_account_db_access(username: str, paths) -> AccountDbAccess:
    """Provision the role and return connection details with a running proxy."""
    database, role, password = await provision_account_db(username)
    socket_dir = _socket_dir_for(paths)
    socket_path = socket_dir / _SOCKET_NAME
    access = AccountDbAccess(
        username=username,
        database=database,
        role=role,
        password=password,
        socket_dir=socket_dir,
        socket_path=socket_path,
    )
    async with _PROXY_LOCK:
        proxy = _PROXIES.get(username)
        if (
            proxy is None
            or proxy.access.database != database
            or proxy.access.role != role
            or not proxy.access.socket_path.exists()
        ):
            if proxy is not None:
                await proxy.stop()
            proxy = PostgresProxy(
                access,
                _app_db_config()["host"],
                _app_db_config()["port"],
            )
            await proxy.start()
            _PROXIES[username] = proxy
    # Ownership of the socket directory must match the sandbox UID.  The caller
    # prepares output ownership before invoking this; setting it here is
    # idempotent and covers a directory recreated after an output cleanup.
    try:
        uid = int(paths.identity.uid)
        gid = int(paths.identity.gid)
        os.chown(socket_dir, uid, gid)
        os.chmod(socket_dir, 0o700)
        os.chown(socket_path, uid, gid)
        try:
            os.chmod(socket_path, 0o660)
        except OSError as exc:
            if exc.errno == errno.EINVAL:
                logger.debug("sandbox db: chmod socket unsupported", exc_info=True)
            else:
                logger.warning("sandbox db: could not chmod socket", exc_info=True)
    except OSError:
        logger.warning("sandbox db: could not set socket permissions", exc_info=True)
    return access


def pg_environment(access: AccountDbAccess) -> dict[str, str]:
    """Standard libpq environment pointing at the account's UNIX socket."""
    return {
        "PGHOST": str(access.socket_dir),
        "PGPORT": str(_SOCKET_PORT),
        "PGUSER": access.role,
        "PGPASSWORD": access.password,
        "PGDATABASE": access.database,
        "PGSSLMODE": "disable",
        "HUIZHI_PG_SOCKET_DIR": str(access.socket_dir),
    }
