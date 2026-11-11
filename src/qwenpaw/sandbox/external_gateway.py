# -*- coding: utf-8 -*-
"""Controlled external data/API gateway for the ordinary-user sandbox.

The sandbox has no TCP network.  When a business workflow needs a third-party
database or API, user code talks to this gateway over a UNIX socket; the
gateway runs in the application process, uses administrator-configured sources,
and returns only query results / HTTP responses.

Security rules
--------------
* External SQL is read-only (single ``SELECT``/``WITH``, no data-modifying CTE).
* Sources, credentials and HTTP allow-list live in
  ``<SECRET_DIR>/sandbox-external-sources.json`` (root-only).  User code never
  sees a connection string.
* Every source declares who may use it (``allow_all`` or ``allowed_users``).
  A source with neither is denied.
* HTTP is limited to explicitly allow-listed hosts, no redirects, bounded body.
* The gateway socket lives under the account's ``outputs/`` and is owned by the
  sandbox UID; Landlock keeps other accounts out.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import datetime as _dt
import decimal
import hashlib
import json
import logging
import os
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .audit import audit_path

logger = logging.getLogger(__name__)

_SOCKET_NAME = "gateway.sock"
_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_ROWS = 1000
_DEFAULT_ROWS = 200
_QUERY_TIMEOUT = 30.0
_HTTP_TIMEOUT = 30.0
_MAX_HTTP_BODY = 2 * 1024 * 1024
_MAX_CONCURRENT = 8

_READ_ONLY_SQL_RE = re.compile(
    r"^\s*(?:SELECT|WITH)\b",
    re.IGNORECASE | re.DOTALL,
)
_BLOCKED_SQL_RE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|TRUNCATE|ALTER|DROP|CREATE|GRANT|"
    r"REVOKE|COPY|CALL|DO|EXECUTE|INTO\s+OUTFILE|LOAD_FILE|DBLINK|"
    r"PG_READ_FILE|PG_READ_BINARY_FILE|LO_IMPORT|LO_EXPORT|XP_CMDSHELL|"
    r"UTL_HTTP|DBMS_)\b",
    re.IGNORECASE,
)
_BLOCKED_HTTP_METHODS = {"CONNECT", "TRACE", "TRACK"}

_CLIENT_SOURCE = '''\
"""Tiny client for the 汇智 sandbox external-data gateway.

Usage (inside sandboxed user code):

    import os, sys
    sys.path.insert(0, os.environ["HUIZHI_OUTPUT_DIR"])
    import huizhi_gateway

    print(huizhi_gateway.sources())
    rows = huizhi_gateway.query("erp_pg", "SELECT * FROM orders LIMIT 10")
    resp = huizhi_gateway.http("GET", "https://api.example.com/health")
"""

import json
import os
import socket

_SOCKET = os.environ.get("HUIZHI_GATEWAY_SOCKET", "")


def _call(payload, timeout=60.0):
    if not _SOCKET:
        raise RuntimeError("沙箱外部数据网关未配置")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\\n"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(_SOCKET)
        sock.sendall(data)
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        sock.close()
    result = json.loads(b"".join(chunks).decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "网关调用失败")
    return result.get("data")


def sources():
    return _call({"op": "sources"})


def query(source, sql, params=None, limit=200):
    return _call(
        {"op": "query", "source": source, "sql": sql, "params": params, "limit": limit},
        timeout=90.0,
    )


def http(method, url, headers=None, body=None, timeout=30.0):
    return _call(
        {
            "op": "http",
            "method": method,
            "url": url,
            "headers": headers or {},
            "body": body,
            "timeout": timeout,
        },
        timeout=float(timeout) + 30.0,
    )
'''


class ExternalGatewayError(RuntimeError):
    """A gateway request was refused or failed."""


@dataclass(frozen=True)
class GatewayPaths:
    username: str
    socket_dir: Path
    socket_path: Path
    client_path: Path
    uid: int
    gid: int


def _secret_config_path() -> Path:
    from ..constant import SECRET_DIR

    return Path(SECRET_DIR) / "sandbox-external-sources.json"


def _load_config() -> dict:
    path = _secret_config_path()
    try:
        if path.stat().st_mode & 0o077:
            logger.warning(
                "external gateway: %s must be root-only (0600); refusing it",
                path,
            )
            return {"sources": {}, "http_allow": []}
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sources": {}, "http_allow": []}
    if not isinstance(data, dict):
        return {"sources": {}, "http_allow": []}
    sources = data.get("sources")
    if not isinstance(sources, dict):
        sources = {}
    allow = data.get("http_allow")
    if not isinstance(allow, list):
        allow = []
    return {"sources": sources, "http_allow": [str(item) for item in allow]}


def _safe_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    return str(value)


def _query_sql_guard(sql: str) -> str:
    text = (sql or "").strip()
    if not text:
        raise ExternalGatewayError("SQL 不能为空")
    # Guard a scrubbed copy; execute the original (comments/string literals
    # must not be rewritten for the real query).
    guard = re.sub(r"'(?:[^']|'')*'", "''", text)
    guard = re.sub(r"/\*.*?\*/", " ", guard, flags=re.DOTALL)
    guard = re.sub(r"--[^\n]*", " ", guard)
    stripped = guard.rstrip().rstrip(";").strip()
    if not stripped or ";" in stripped:
        raise ExternalGatewayError("只允许单条只读 SQL")
    if not _READ_ONLY_SQL_RE.match(stripped):
        raise ExternalGatewayError("外部数据源只允许 SELECT/WITH 查询")
    if _BLOCKED_SQL_RE.search(stripped):
        raise ExternalGatewayError("查询包含不允许的写操作/敏感函数")
    return text.rstrip().rstrip(";").strip()


def _source_for(username: str, name: str) -> dict:
    cfg = _load_config()
    sources = cfg["sources"]
    source = sources.get(name)
    if not isinstance(source, dict):
        raise ExternalGatewayError(f"未配置数据源：{name}")
    if source.get("allow_all") is True:
        return source
    allowed = source.get("allowed_users")
    if isinstance(allowed, list) and username in {str(item) for item in allowed}:
        return source
    raise ExternalGatewayError(f"当前账号无权使用数据源：{name}")


def _public_sources(username: str) -> list[dict]:
    cfg = _load_config()
    out: list[dict] = []
    for name, source in cfg["sources"].items():
        if not isinstance(source, dict):
            continue
        allowed = source.get("allow_all") is True
        if not allowed:
            users = source.get("allowed_users")
            allowed = isinstance(users, list) and username in {
                str(item) for item in users
            }
        if not allowed:
            continue
        out.append(
            {
                "name": name,
                "type": str(source.get("type") or ""),
            },
        )
    return out


def _connect_external(source: dict):
    kind = str(source.get("type") or "").strip().lower()
    if kind in {"postgres", "postgresql", "pg"}:
        import psycopg2

        conn = psycopg2.connect(
            host=str(source.get("host") or ""),
            port=int(source.get("port") or 5432),
            dbname=str(source.get("database") or source.get("dbname") or ""),
            user=str(source.get("user") or source.get("username") or ""),
            password=str(source.get("password") or ""),
            connect_timeout=int(source.get("connect_timeout") or 5),
        )
        conn.set_session(readonly=True, autocommit=False)
        return "pg", conn
    if kind in {"mysql", "mariadb"}:
        import pymysql

        conn = pymysql.connect(
            host=str(source.get("host") or ""),
            port=int(source.get("port") or 3306),
            user=str(source.get("user") or source.get("username") or ""),
            password=str(source.get("password") or ""),
            database=str(source.get("database") or ""),
            connect_timeout=int(source.get("connect_timeout") or 5),
            read_timeout=int(source.get("read_timeout") or 30),
            autocommit=False,
            charset=str(source.get("charset") or "utf8mb4"),
        )
        return "mysql", conn
    if kind in {"oracle", "oracledb"}:
        import oracledb

        conn = oracledb.connect(
            user=str(source.get("user") or source.get("username") or ""),
            password=str(source.get("password") or ""),
            dsn=str(source.get("dsn") or ""),
        )
        conn.autocommit = False
        return "oracle", conn
    if kind in {"hive", "hiveserver2"}:
        from pyhive import hive

        conn = hive.Connection(
            host=str(source.get("host") or ""),
            port=int(source.get("port") or 10000),
            username=str(source.get("user") or source.get("username") or ""),
            database=str(source.get("database") or "default"),
            auth=str(source.get("auth") or "NOSASL"),
        )
        return "hive", conn
    raise ExternalGatewayError(f"不支持的数据源类型：{kind}")


def _run_query(source: dict, sql: str, params: Any, limit: int) -> dict:
    sql = _query_sql_guard(sql)
    limit = max(1, min(int(limit or _DEFAULT_ROWS), _MAX_ROWS))
    kind, conn = _connect_external(source)
    try:
        cur = conn.cursor()
        try:
            if kind == "pg":
                cur.execute("SET LOCAL statement_timeout = '30s'")
            elif kind == "mysql":
                cur.execute("SET SESSION MAX_EXECUTION_TIME=30000")
                cur.execute("START TRANSACTION READ ONLY")
            elif kind == "oracle":
                cur.execute("SET TRANSACTION READ ONLY")
            if params is None:
                cur.execute(sql)
            else:
                cur.execute(sql, params)
            columns = [str(item[0]) for item in (cur.description or [])]
            rows = cur.fetchmany(limit + 1)
            truncated = len(rows) > limit
            rows = rows[:limit]
            return {
                "columns": columns,
                "rows": [[_safe_json(v) for v in row] for row in rows],
                "truncated": truncated,
                "row_limit": limit,
            }
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _host_allowed(host: str, allow: list[str]) -> bool:
    host = host.lower().strip().rstrip(".")
    for entry in allow:
        item = entry.lower().strip().rstrip(".")
        if not item:
            continue
        if item.startswith("*."):
            if host.endswith(item[1:]) and host != item[2:]:
                return True
        elif host == item:
            return True
    return False


async def _http_request(payload: dict) -> dict:
    import httpx

    method = str(payload.get("method") or "GET").upper()
    if method in _BLOCKED_HTTP_METHODS:
        raise ExternalGatewayError("不允许的 HTTP 方法")
    url = str(payload.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ExternalGatewayError("只允许 http/https URL")
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "").lower()
    allow = _load_config()["http_allow"]
    if not _host_allowed(host, allow):
        raise ExternalGatewayError(f"HTTP 目标不在白名单：{host}")
    headers = payload.get("headers")
    if not isinstance(headers, dict):
        headers = {}
    body = payload.get("body")
    timeout = min(max(float(payload.get("timeout") or _HTTP_TIMEOUT), 1.0), 120.0)
    request_kwargs: dict[str, Any] = {
        "headers": {str(k): str(v) for k, v in headers.items()},
    }
    if isinstance(body, (dict, list)):
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if len(encoded) > _MAX_HTTP_BODY:
            raise ExternalGatewayError("HTTP 请求体超过 2MB")
        request_kwargs["json"] = body
    elif isinstance(body, str):
        encoded = body.encode("utf-8")
        if len(encoded) > _MAX_HTTP_BODY:
            raise ExternalGatewayError("HTTP 请求体超过 2MB")
        request_kwargs["content"] = encoded
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
    ) as client:
        async with client.stream(method, url, **request_kwargs) as response:
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > _MAX_HTTP_BODY:
                    raise ExternalGatewayError("HTTP 响应超过 2MB")
                chunks.append(chunk)
            raw = b"".join(chunks)
            content_type = response.headers.get("content-type", "")
            if content_type.startswith("text/") or "json" in content_type:
                body_text = raw.decode("utf-8", errors="replace")
                body_kind = "text"
            else:
                body_text = base64.b64encode(raw).decode("ascii")
                body_kind = "base64"
            return {
                "status": response.status_code,
                "headers": dict(response.headers),
                "body": body_text,
                "body_encoding": body_kind,
            }


def _audit_gateway(username: str, op: str, detail: dict, result: str) -> None:
    try:
        path = audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "kind": "external_gateway",
            "user": username,
            "op": op,
            "detail": detail,
            "result": result,
        }
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001
        logger.warning("external gateway: audit failed", exc_info=True)


class _GatewayServer:
    def __init__(self, username: str, paths: GatewayPaths):
        self.username = username
        self.paths = paths
        self._server: Optional[asyncio.AbstractServer] = None
        self._clients = 0

    async def start(self) -> None:
        if self.paths.socket_dir.is_symlink():
            self.paths.socket_dir.unlink()
        self.paths.socket_dir.mkdir(parents=True, exist_ok=True)
        if self.paths.socket_path.exists():
            try:
                self.paths.socket_path.unlink()
            except OSError:
                pass
        self._server = await asyncio.start_unix_server(
            self._handle,
            path=str(self.paths.socket_path),
            backlog=16,
            limit=_MAX_REQUEST_BYTES + 1,
        )
        try:
            os.chown(self.paths.socket_path, self.paths.uid, self.paths.gid)
        except OSError:
            logger.warning("external gateway: could not chown socket", exc_info=True)
        try:
            os.chmod(self.paths.socket_path, 0o660)
        except OSError as exc:
            if exc.errno == errno.EINVAL:
                logger.debug("external gateway: chmod socket unsupported", exc_info=True)
            else:
                logger.warning("external gateway: could not chmod socket", exc_info=True)
        try:
            if self.paths.client_path.is_symlink():
                self.paths.client_path.unlink()
            self.paths.client_path.write_text(_CLIENT_SOURCE, "utf-8")
            os.chown(self.paths.client_path, self.paths.uid, self.paths.gid)
            os.chmod(self.paths.client_path, 0o644)
        except OSError:
            logger.warning("external gateway: could not write client", exc_info=True)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if self._clients >= _MAX_CONCURRENT:
            writer.close()
            return
        self._clients += 1
        try:
            try:
                raw = await asyncio.wait_for(
                    reader.readline(),
                    timeout=10,
                )
            except asyncio.TimeoutError:
                return
            if not raw or len(raw) > _MAX_REQUEST_BYTES:
                return
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                response = {"ok": False, "error": "invalid JSON"}
            else:
                response = await self._dispatch(payload)
            writer.write(
                (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"),
            )
            await writer.drain()
        finally:
            self._clients -= 1
            try:
                writer.close()
            except OSError:
                pass

    async def _dispatch(self, payload: Any) -> dict:
        if not isinstance(payload, dict):
            return {"ok": False, "error": "request must be an object"}
        op = str(payload.get("op") or "")
        try:
            if op == "sources":
                return {"ok": True, "data": _public_sources(self.username)}
            if op == "query":
                source_name = str(payload.get("source") or "")
                source = _source_for(self.username, source_name)
                sql = str(payload.get("sql") or "")
                params = payload.get("params")
                limit = int(payload.get("limit") or _DEFAULT_ROWS)
                data = await asyncio.to_thread(
                    _run_query,
                    source,
                    sql,
                    params,
                    limit,
                )
                _audit_gateway(
                    self.username,
                    "query",
                    {
                        "source": source_name,
                        "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
                        "rows": len(data.get("rows") or []),
                    },
                    "ok",
                )
                return {"ok": True, "data": data}
            if op == "http":
                data = await _http_request(payload)
                _audit_gateway(
                    self.username,
                    "http",
                    {
                        "method": str(payload.get("method") or "GET"),
                        "url_sha256": hashlib.sha256(
                            str(payload.get("url") or "").encode(),
                        ).hexdigest(),
                        "status": data.get("status"),
                    },
                    "ok",
                )
                return {"ok": True, "data": data}
            return {"ok": False, "error": f"unknown op: {op}"}
        except ExternalGatewayError as exc:
            _audit_gateway(self.username, op or "?", {}, f"denied: {exc}")
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("external gateway request failed", exc_info=True)
            _audit_gateway(self.username, op or "?", {}, f"error: {type(exc).__name__}")
            return {"ok": False, "error": f"gateway error: {type(exc).__name__}"}


@dataclass(frozen=True)
class GatewayHandle:
    socket_path: Path
    client_path: Path


_PROXY_LOCK = asyncio.Lock()
_GATEWAYS: dict[str, _GatewayServer] = {}


async def ensure_external_gateway(username: str, paths, uid: int, gid: int) -> GatewayHandle:
    """Start the gateway socket and write the client library into scripts/."""
    socket_dir = paths.output_dir / "gateway"
    if socket_dir.is_symlink():
        socket_dir.unlink()
    socket_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chown(socket_dir, uid, gid)
        os.chmod(socket_dir, 0o700)
    except OSError:
        logger.warning("external gateway: socket dir permissions", exc_info=True)
    gateway_paths = GatewayPaths(
        username=username,
        socket_dir=socket_dir,
        socket_path=socket_dir / _SOCKET_NAME,
        client_path=paths.scripts_dir / "huizhi_gateway.py",
        uid=uid,
        gid=gid,
    )
    async with _PROXY_LOCK:
        gateway = _GATEWAYS.get(username)
        if gateway is None or not gateway.paths.socket_path.exists():
            if gateway is not None:
                await gateway.stop()
            gateway = _GatewayServer(username, gateway_paths)
            await gateway.start()
            _GATEWAYS[username] = gateway
    return GatewayHandle(
        socket_path=gateway_paths.socket_path,
        client_path=gateway_paths.client_path,
    )
