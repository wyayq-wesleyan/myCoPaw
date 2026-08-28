# 数据分析模块实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现数据录入→瀚高入库→自然语言SQL查询的完整链路，第一期包含建表/导入/Agent工具/前端页面。

**Architecture:** 主应用新增 `data_tables` 模块，元数据存系统 PG17，业务数据存瀚高（按用户建库隔离）。前端新增「数据分析」菜单。Agent 通过三个工具（list/describe/query）将自然语言转为 SQL。

**Tech Stack:** psycopg2-binary, SQLAlchemy, pandas, openpyxl, charset-normalizer, @ant-design/plots, mermaid

## Global Constraints

- 瀚高安全版 v4.5（PG 内核），用 psycopg2 直连
- 表名校验正则：`^tb_[a-z][a-z0-9_]{0,62}$`
- 列名校验正则：`^[a-z][a-z0-9_]{0,62}$`
- SQL 查询强制只读（SELECT/WITH），超时 30s，限 100 行
- 导入文件单文件 ≤ 100MB，**支持多文件同时上传**，每个文件独立创建异步任务
- 导入并发：每个用户最多 3 个并发导入任务
- 密码字段加密存储（复用 encrypt_dict_fields）

---

## Task 1: 数据库 Schema 初始化

**Files:**
- Create: `src/qwenpaw/data_tables/__init__.py` — 空文件（包标识）
- Create: `src/qwenpaw/data_tables/store.py`

**Interfaces:**
- Produces: `init_data_tables_schema()` — 在系统 PG17 创建三张元数据表

- [ ] **Step 1: 创建 data_tables 包**

```bash
mkdir -p src/qwenpaw/data_tables
touch src/qwenpaw/data_tables/__init__.py
```

- [ ] **Step 2: 创建 store.py 和 schema 初始化函数**

```python
# src/qwenpaw/data_tables/store.py
"""Data tables module — metadata storage in system PG17."""

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from ..constant import WORKING_DIR
from ..config.config import load_config

_pool: ThreadedConnectionPool | None = None


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    cfg = load_config()
    db_url = cfg.database_url or f"postgresql://postgres:postgres@localhost:5432/{cfg.database_name or 'qwenpaw'}"
    # Parse connection string for pool
    import urllib.parse
    parsed = urllib.parse.urlparse(db_url)
    _pool = ThreadedConnectionPool(
        1, 10,
        host=parsed.hostname or "localhost",
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/") or "qwenpaw",
        user=parsed.username or "postgres",
        password=parsed.password or "",
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
            cur.execute("""
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
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS data_table_registry (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    owner_username TEXT NOT NULL,
                    db_name TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    columns JSONB NOT NULL DEFAULT '[]',
                    row_count BIGINT DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    updated_at TIMESTAMPTZ DEFAULT now(),
                    UNIQUE(owner_username, table_name)
                );
            """)
            cur.execute("""
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
            """)
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)
```

- [ ] **Step 3: 验证函数存在且可导入**

Run: `docker exec huizhi-v210-check python -c "from qwenpaw.data_tables.store import init_data_tables_schema; print('OK')"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add src/qwenpaw/data_tables/store.py
git commit -m "feat(data): add metadata store schema for data analysis module"
```

---

## Task 2: 数据源配置 API（管理端）

**Files:**
- Create: `src/qwenpaw/data_tables/config_api.py`
- Modify: `src/qwenpaw/app/_app.py` — 注册路由
- Modify: `src/qwenpaw/app/user_space.py` — DEFAULT_USER_MENU_IDS 加 core.data-analysis

**Interfaces:**
- Produces: `GET/PUT/POST /api/data-source` — 管理端数据源配置接口

- [ ] **Step 1: 创建 config_api.py**

```python
# src/qwenpaw/data_tables/config_api.py
"""Data source configuration API for admin settings."""

import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..app.auth import require_admin_access
from ..app.user_space import get_request_username
from .store import _get_conn, _put_conn

router = APIRouter(prefix="/api/data-source", tags=["data-source"])


class DataSourceConfig(BaseModel):
    name: str = Field(default="highgo_default", pattern=r"^[a-z][a-z0-9_]{0,31}$")
    host: str
    port: int = Field(default=5432, ge=1, le=65535)
    username: str
    password: str  # plaintext in request, encrypted before storage


class DataSourceResponse(BaseModel):
    name: str
    host: str
    port: int
    username: str
    password_masked: str


def _mask_password(pw: str) -> str:
    if len(pw) <= 4:
        return "****"
    return pw[:2] + "*" * (len(pw) - 4) + pw[-2:]


@router.get("", response_model=DataSourceResponse)
async def get_data_source(request: Request):
    username = get_request_username(request)
    require_admin_access(username)
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM data_source_config ORDER BY created_at DESC LIMIT 1")
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="数据源未配置")
            return DataSourceResponse(
                name=row["name"],
                host=row["host"],
                port=row["port"],
                username=row["username"],
                password_masked=_mask_password(row["password_encrypted"]),
            )
    finally:
        _put_conn(conn)


@router.put("")
async def update_data_source(payload: DataSourceConfig, request: Request):
    username = get_request_username(request)
    require_admin_access(username)
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO data_source_config (name, host, port, username, password_encrypted, updated_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (name) DO UPDATE SET
                    host=EXCLUDED.host, port=EXCLUDED.port,
                    username=EXCLUDED.username, password_encrypted=EXCLUDED.password_encrypted,
                    updated_at=now()
            """, (payload.name, payload.host, payload.port, payload.username, payload.password))
            conn.commit()
        return {"ok": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)


@router.post("/test")
async def test_data_source(request: Request):
    username = get_request_username(request)
    require_admin_access(username)
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM data_source_config ORDER BY created_at DESC LIMIT 1")
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="数据源未配置")
    finally:
        _put_conn(conn)
    # Test actual connection to target DB
    import psycopg2 as pg2
    try:
        test_conn = pg2.connect(
            host=row["host"], port=row["port"], user=row["username"], password=row["password_encrypted"],
            connect_timeout=5,
        )
        test_conn.close()
        return {"ok": True, "message": "连接成功"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"连接失败: {str(e)}")
```

- [ ] **Step 2: 注册路由到统一 API 路由**

在 `src/qwenpaw/app/routers/__init__.py` 中添加：
```python
from ...data_tables.config_api import router as data_source_router
```
然后在 `router.include_router(...)` 调用中添加：
```python
router.include_router(data_source_router)
```

- [ ] **Step 3: DEFAULT_USER_MENU_IDS 加入 core.data-analysis**

修改 `src/qwenpaw/app/user_space.py` 第 42-55 行的 `DEFAULT_USER_MENU_IDS` frozenset，加入 `"core.data-analysis"`。同时修改 `get_menu_permissions` 的 force-include union（第 457-459 行）加入 `"core.data-analysis"`。

- [ ] **Step 4: 验证 API**

Run: `curl -s http://localhost:18088/api/data-source -H "Authorization: Bearer $TOKEN"`
Expected: 404 (未配置) 或返回数据源信息

- [ ] **Step 5: Commit**

```bash
git add src/qwenpaw/data_tables/config_api.py src/qwenpaw/app/_app.py src/qwenpaw/app/user_space.py
git commit -m "feat(data): add data source config API and menu registration"
```

---

## Task 3: 瀚高连接管理器

**Files:**
- Create: `src/qwenpaw/data_tables/connection.py`

**Interfaces:**
- Produces: `get_target_connection(db_name)` — 返回到瀚高指定库的连接
- Produces: `ensure_user_database(username)` — 确保用户库存在
- Produces: `list_user_databases()` — 列出所有用户库

- [ ] **Step 1: 创建 connection.py**

```python
# src/qwenpaw/data_tables/connection.py
"""HighGo database connection manager."""

import re
import logging
from typing import Optional

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger(__name__)

_target_pool: dict[str, ThreadedConnectionPool] = {}
_source_config: dict | None = None


def _slug_username(username: str) -> str:
    """Slugify username for database name: lowercase, alphanumeric + underscore only."""
    slug = re.sub(r"[^a-z0-9_]", "_", username.lower().strip())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug[:63] or "db_default"


def _load_source_config():
    """Load data source config from system PG17."""
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
    """Get a connection to the target HighGo database."""
    if db_name in _target_pool:
        pool = _target_pool[db_name]
    else:
        cfg = _get_source_config()
        pool = ThreadedConnectionPool(
            1, 5,
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
    """Return connection to pool."""
    if db_name in _target_pool:
        _target_pool[db_name].putconn(conn)


def ensure_user_database(username: str) -> str:
    """Ensure user database exists in HighGo. Returns db_name."""
    db_name = _slug_username(username)
    cfg = _get_source_config()
    # Connect to default 'postgres' db to create database
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
    """Create a table in the target HighGo database."""
    conn = get_target_connection(db_name)
    try:
        with conn.cursor() as cur:
            # Build CREATE TABLE statement
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
            # Add column comments
            for col in columns:
                if col.get("comment"):
                    cur.execute(
                        f'COMMENT ON COLUMN "{table_name}"."{col["name"]}" IS %s',
                        (col["comment"],),
                    )
            # Add table comment
            cur.execute(f'COMMENT ON TABLE "{table_name}" IS %s', (table_name,))
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_target_connection(conn, db_name)


def drop_table(db_name: str, table_name: str) -> None:
    """Drop a table in the target database."""
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
    """Truncate a table in the target database."""
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
```

- [ ] **Step 2: 验证函数存在**

Run: `docker exec huizhi-v210-check python -c "from qwenpaw.data_tables.connection import ensure_user_database, get_target_connection; print('OK')"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add src/qwenpaw/data_tables/connection.py
git commit -m "feat(data): add HighGo connection manager with per-user database isolation"
```

---

## Task 4: 表管理 API

**Files:**
- Create: `src/qwenpaw/data_tables/table_api.py`
- Modify: `src/qwenpaw/app/_app.py` — 注册路由

**Interfaces:**
- Produces: `GET/POST/DELETE /api/data-tables` — 表 CRUD + 分页列表

- [ ] **Step 1: 创建 table_api.py**

```python
# src/qwenpaw/data_tables/table_api.py
"""Data table management API."""

import re
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..app.user_space import get_request_username, is_admin_user
from .connection import (
    ensure_user_database, get_target_connection, put_target_connection,
    init_user_tables, drop_table, _slug_username,
)
from .store import _get_conn, _put_conn

router = APIRouter(prefix="/api/data-tables", tags=["data-tables"])

TABLE_NAME_RE = re.compile(r"^tb_[a-z][a-z0-9_]{0,62}$")
COLUMN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class ColumnDef(BaseModel):
    name: str
    type: str = Field(..., pattern=r"^(TEXT|INTEGER|BIGINT|NUMERIC\(\d+,\d+\)|DATE|TIMESTAMP|BOOLEAN)$")
    comment: str
    nullable: bool = True
    is_primary_key: bool = False


class CreateTableRequest(BaseModel):
    table_name: str
    description: str = ""
    columns: list[ColumnDef]


class TableListItem(BaseModel):
    id: str
    table_name: str
    description: str
    column_count: int
    row_count: int
    created_at: str
    updated_at: str


class TableDetail(BaseModel):
    id: str
    table_name: str
    description: str
    columns: list[dict]
    row_count: int
    created_at: str
    updated_at: str


@router.get("", response_model=dict)
async def list_tables(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: Optional[str] = Query(None),
    created_start: Optional[str] = Query(None),
    created_end: Optional[str] = Query(None),
):
    username = get_request_username(request) or "admin"
    is_admin = is_admin_user(username)
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=__import__("psycopg2.extras", fromlist=["RealDictCursor"]).RealDictCursor) as cur:
            conditions = []
            params = []
            if not is_admin:
                conditions.append("owner_username = %s")
                params.append(username)
            if keyword:
                conditions.append("(table_name ILIKE %s OR description ILIKE %s)")
                params.extend([f"%{keyword}%", f"%{keyword}%"])
            if created_start:
                conditions.append("created_at >= %s")
                params.append(created_start)
            if created_end:
                conditions.append("created_at <= %s")
                params.append(created_end)
            where = " WHERE " + " AND ".join(conditions) if conditions else ""
            # Count
            cur.execute(f"SELECT COUNT(*) as cnt FROM data_table_registry{where}", params)
            total = cur.fetchone()["cnt"]
            # Page
            offset = (page - 1) * page_size
            cur.execute(
                f"SELECT * FROM data_table_registry{where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
                params + [page_size, offset],
            )
            rows = cur.fetchall()
            items = [
                TableListItem(
                    id=str(r["id"]),
                    table_name=r["table_name"],
                    description=r["description"] or "",
                    column_count=len(r["columns"]) if r["columns"] else 0,
                    row_count=r["row_count"] or 0,
                    created_at=str(r["created_at"]),
                    updated_at=str(r["updated_at"]),
                ).model_dump()
                for r in rows
            ]
            return {"items": items, "total": total, "page": page, "page_size": page_size}
    finally:
        _put_conn(conn)


@router.post("", status_code=201)
async def create_table(payload: CreateTableRequest, request: Request):
    username = get_request_username(request) or "admin"
    # Validate table name
    if not TABLE_NAME_RE.match(payload.table_name):
        raise HTTPException(status_code=400, detail="表名格式错误，需以 tb_ 开头，只含小写字母/数字/下划线")
    # Validate columns
    for col in payload.columns:
        if not COLUMN_NAME_RE.match(col.name):
            raise HTTPException(status_code=400, detail=f"列名格式错误: {col.name}")
    pk_count = sum(1 for c in payload.columns if c.is_primary_key)
    if pk_count > 1:
        raise HTTPException(status_code=400, detail="最多只能有一个主键列")
    # Check uniqueness
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM data_table_registry WHERE owner_username=%s AND table_name=%s", (username, payload.table_name))
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="表名已存在")
    finally:
        _put_conn(conn)
    # Create database + table in HighGo
    db_name = ensure_user_database(username)
    cols = [c.model_dump() for c in payload.columns]
    init_user_tables(db_name, payload.table_name, cols)
    # Save metadata
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            import json
            cur.execute("""
                INSERT INTO data_table_registry (owner_username, db_name, table_name, description, columns)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (username, db_name, payload.table_name, payload.description, json.dumps(cols)))
            row_id = cur.fetchone()[0]
            conn.commit()
        return {"id": str(row_id), "table_name": payload.table_name}
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)


@router.delete("/{table_id}")
async def delete_table(table_id: str, request: Request):
    username = get_request_username(request) or "admin"
    is_adm = is_admin_user(username)
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=__import__("psycopg2.extras", fromlist=["RealDictCursor"]).RealDictCursor) as cur:
            cur.execute("SELECT * FROM data_table_registry WHERE id = %s", (table_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="表不存在")
            if not is_adm and row["owner_username"] != username:
                raise HTTPException(status_code=403, detail="无权删除此表")
            # Drop from HighGo
            drop_table(row["db_name"], row["table_name"])
            # Delete metadata
            cur.execute("DELETE FROM data_table_registry WHERE id = %s", (table_id,))
            conn.commit()
        return {"ok": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)
```

- [ ] **Step 2: 注册路由**

在 `src/qwenpaw/app/routers/__init__.py` 中添加：
```python
from ...data_tables.table_api import router as data_tables_router
```
然后在 `router.include_router(...)` 调用中添加：
```python
router.include_router(data_tables_router)
```

- [ ] **Step 3: 验证 API**

Run: `curl -s http://localhost:18088/api/data-tables?page=1 -H "Authorization: Bearer $TOKEN"`
Expected: `{"items":[],"total":0,"page":1,"page_size":20}`

- [ ] **Step 4: Commit**

```bash
git add src/qwenpaw/data_tables/table_api.py src/qwenpaw/app/_app.py
git commit -m "feat(data): add table CRUD API with pagination and fuzzy search"
```

---

## Task 5: 异步导入 Worker

**Files:**
- Create: `src/qwenpaw/data_tables/importer.py`
- Create: `src/qwenpaw/data_tables/import_api.py`
- Modify: `src/qwenpaw/app/_app.py` — 注册导入路由

**Interfaces:**
- Produces: `start_import_task(table_id, file_path, filename)` — 启动异步导入
- Produces: `GET/POST /api/data-tables/{id}/import` — 上传文件+创建任务
- Produces: `GET /api/data-import-jobs` — 任务列表

- [ ] **Step 1: 创建 importer.py（核心导入逻辑）**

```python
# src/qwenpaw/data_tables/importer.py
"""Async data importer — parse files and load into HighGo."""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import psycopg2
import psycopg2.extras

from .connection import get_target_connection, put_target_connection
from .store import _get_conn, _put_conn

logger = logging.getLogger(__name__)

# Date formats to try
DATE_FORMATS = [
    "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d",
    "%d/%m/%Y", "%m/%d/%Y",
    "%Y年%m月%d日",
]

TIMESTAMP_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M:%S", "%Y%m%d %H:%M:%S",
    "%Y年%m月%d日 %H:%M:%S",
]


def _detect_encoding(file_path: str) -> str:
    """Detect CSV file encoding."""
    try:
        from charset_normalizer import from_bytes
        with open(file_path, "rb") as f:
            raw = f.read(10000)
        result = from_bytes(raw).best()
        if result and result.encoding:
            enc = result.encoding.lower()
            if enc in ("utf-8", "ascii"):
                return "utf-8"
            if "gb" in enc:
                return "gbk"
            return result.encoding
    except Exception:
        pass
    # Fallback: try utf-8 then gbk
    return "utf-8"


def _parse_value(value: Any, col_type: str) -> Any:
    """Parse a value according to column type."""
    if pd.isna(value) or value == "" or value is None:
        return None
    try:
        if col_type == "TEXT":
            return str(value).strip()
        elif col_type == "INTEGER":
            return int(float(str(value).strip()))
        elif col_type == "BIGINT":
            return int(float(str(value).strip()))
        elif col_type.startswith("NUMERIC"):
            return float(str(value).strip())
        elif col_type == "BOOLEAN":
            v = str(value).strip().lower()
            return v in ("true", "1", "是", "yes", "y")
        elif col_type == "DATE":
            s = str(value).strip()
            for fmt in DATE_FORMATS:
                try:
                    return datetime.strptime(s, fmt).date()
                except ValueError:
                    continue
            return None
        elif col_type == "TIMESTAMP":
            s = str(value).strip()
            for fmt in TIMESTAMP_FORMATS:
                try:
                    return datetime.strptime(s, fmt)
                except ValueError:
                    continue
            return None
    except Exception:
        return None
    return str(value).strip()


def _process_file(file_path: str, filename: str, columns: list[dict]) -> tuple[pd.DataFrame, list[dict]]:
    """Parse file into DataFrame, return (df, errors)."""
    errors = []
    ext = os.path.splitext(filename)[1].lower()
    try:
        if ext == ".csv":
            enc = _detect_encoding(file_path)
            df = pd.read_csv(file_path, encoding=enc, dtype=str, keep_default_na=False)
        elif ext in (".xls", ".xlsx"):
            df = pd.read_excel(file_path, dtype=str, keep_default_na=False)
        else:
            raise ValueError(f"不支持的文件格式: {ext}")
    except Exception as e:
        raise ValueError(f"文件解析失败: {str(e)}")

    # Map columns
    col_map = {c["name"]: c for c in columns}
    # Try to match by column comment or name
    # For now, expect CSV headers to match column names
    missing_cols = [c["name"] for c in columns if c["name"] not in df.columns]
    if missing_cols:
        errors.append({"row": 0, "column": ",", ".join(missing_cols), "message": f"缺少列: {', '.join(missing_cols)}"})

    # Parse values
    parsed_rows = []
    for idx, row in df.iterrows():
        parsed = {}
        has_error = False
        for col in columns:
            cname = col["name"]
            raw_val = row.get(cname, None)
            parsed_val = _parse_value(raw_val, col["type"])
            parsed[cname] = parsed_val
        parsed_rows.append(parsed)
        if len(errors) >= 100:
            break

    return pd.DataFrame(parsed_rows), errors


def _batch_insert(conn, table_name: str, columns: list[dict], df: pd.DataFrame, batch_size: int = 1000) -> tuple[int, list[dict]]:
    """Insert DataFrame into table in batches. Returns (success_count, errors)."""
    col_names = [c["name"] for c in columns]
    placeholders = ", ".join(["%s"] * len(col_names))
    insert_sql = f'INSERT INTO "{table_name}" ({", ".join(f"{c}" for c in col_names)}) VALUES ({placeholders})'
    success = 0
    errors = []
    for start in range(0, len(df), batch_size):
        batch = df.iloc[start:start + batch_size]
        values = [tuple(row) for _, row in batch.iterrows()]
        try:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, insert_sql, values, page_size=batch_size)
            conn.commit()
            success += len(batch)
        except Exception as e:
            conn.rollback()
            errors.append({"row": start, "column": "", "message": f"批次写入失败: {str(e)[:200]}"})
            if len(errors) >= 100:
                break
    return success, errors


async def run_import_job(job_id: str, table_id: str, file_path: str, filename: str):
    """Main async import task."""
    from .store import _get_conn as get_sys_conn, _put_conn as put_sys_conn
    conn = get_sys_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE data_import_job SET status='parsing' WHERE id=%s", (job_id,))
            conn.commit()
        # Get table metadata
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM data_table_registry WHERE id=%s", (table_id,))
            table = cur.fetchone()
            if not table:
                raise RuntimeError(f"表不存在: {table_id}")
            columns = table["columns"]
            db_name = table["db_name"]
            table_name = table["table_name"]
    finally:
        put_sys_conn(conn)

    try:
        df, parse_errors = _process_file(file_path, filename, columns)
        total = len(df)
        # Update status
        conn = get_sys_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE data_import_job SET status='loading', total_rows=%s WHERE id=%s", (total, job_id))
                conn.commit()
        finally:
            put_sys_conn(conn)
        # Insert
        hg_conn = get_target_connection(db_name)
        try:
            success, insert_errors = _batch_insert(hg_conn, table_name, columns, df)
            all_errors = parse_errors + insert_errors
            status = "completed" if not all_errors else ("partial" if success > 0 else "failed")
        finally:
            put_target_connection(hg_conn, db_name)
        # Update job
        conn = get_sys_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE data_import_job SET
                        status=%s, success_rows=%s, failed_rows=%s, errors=%s, finished_at=now()
                    WHERE id=%s
                """, (status, success, total - success, json.dumps(all_errors[:100]), job_id))
                # Update table row_count
                cur.execute("UPDATE data_table_registry SET row_count = row_count + %s, updated_at=now() WHERE id=%s", (success, table_id))
                conn.commit()
        finally:
            put_sys_conn(conn)
        # Cleanup file
        try:
            os.remove(file_path)
        except OSError:
            pass
    except Exception as e:
        conn = get_sys_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE data_import_job SET status='failed', errors=%s, finished_at=now() WHERE id=%s",
                           (json.dumps([{"row": 0, "column": "", "message": str(e)[:500]}]), job_id))
                conn.commit()
        finally:
            put_sys_conn(conn)


async def start_import_task(table_id: str, file_path: str, filename: str) -> str:
    """Create import job and start async task. Returns job_id."""
    job_id = str(uuid.uuid4())
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO data_import_job (id, table_id, filename, status)
                VALUES (%s, %s, %s, 'pending')
            """, (job_id, table_id, filename))
            conn.commit()
    finally:
        _put_conn(conn)
    # Start async task
    asyncio.create_task(run_import_job(job_id, table_id, file_path, filename))
    return job_id
```

- [ ] **Step 2: 创建 import_api.py**

```python
# src/qwenpaw/data_tables/import_api.py
"""Data import API — file upload and job management."""

import os
import uuid
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile

from ..app.user_space import get_request_username, is_admin_user
from .importer import start_import_task
from .store import _get_conn, _put_conn

router = APIRouter(tags=["data-import"])

UPLOAD_DIR = "/app/working/data_uploads"
ALLOWED_EXTENSIONS = {".csv", ".xls", ".xlsx"}


@router.post("/api/data-tables/{table_id}/import")
async def import_data(table_id: str, files: list[UploadFile] = File(...), request: Request = None):
    """Accept one or more files for async import into the target table."""
    username = get_request_username(request) or "admin"
    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个文件")
    # Validate table exists and belongs to user
    conn = _get_conn()
    try:
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM data_table_registry WHERE id=%s", (table_id,))
            table = cur.fetchone()
            if not table:
                raise HTTPException(status_code=404, detail="表不存在")
            if not is_admin_user(username) and table["owner_username"] != username:
                raise HTTPException(status_code=403, detail="无权操作此表")
    finally:
        _put_conn(conn)
    # Process each file
    job_ids = []
    errors = []
    for file in files:
        ext = os.path.splitext(file.filename or "")[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            errors.append({"filename": file.filename, "error": f"不支持的文件格式: {ext}"})
            continue
        task_id = str(uuid.uuid4())
        task_dir = os.path.join(UPLOAD_DIR, task_id)
        os.makedirs(task_dir, exist_ok=True)
        file_path = os.path.join(task_dir, file.filename or "data.csv")
        content = await file.read()
        with open(file_path, "wb") as f:
            f.write(content)
        job_id = await start_import_task(table_id, file_path, file.filename or "data.csv")
        job_ids.append({"job_id": job_id, "filename": file.filename})
    return {"jobs": job_ids, "errors": errors}


@router.get("/api/data-import-jobs")
async def list_import_jobs(
    request: Request,
    table_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50),
):
    username = get_request_username(request) or "admin"
    is_adm = is_admin_user(username)
    conn = _get_conn()
    try:
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = []
            params = []
            if table_id:
                conditions.append("j.table_id = %s")
                params.append(table_id)
            if not is_adm:
                conditions.append("t.owner_username = %s")
                params.append(username)
            where = ""
            if conditions:
                where = " WHERE " + " AND ".join(conditions)
            cur.execute(f"""
                SELECT COUNT(*) as cnt
                FROM data_import_job j
                JOIN data_table_registry t ON j.table_id = t.id
                {where}
            """, params)
            total = cur.fetchone()["cnt"]
            offset = (page - 1) * page_size
            cur.execute(f"""
                SELECT j.*, t.table_name
                FROM data_import_job j
                JOIN data_table_registry t ON j.table_id = t.id
                {where}
                ORDER BY j.created_at DESC
                LIMIT %s OFFSET %s
            """, params + [page_size, offset])
            rows = cur.fetchall()
            items = [
                {
                    "id": str(r["id"]),
                    "table_id": str(r["table_id"]),
                    "table_name": r["table_name"],
                    "filename": r["filename"],
                    "status": r["status"],
                    "total_rows": r["total_rows"],
                    "success_rows": r["success_rows"],
                    "failed_rows": r["failed_rows"],
                    "errors": r["errors"],
                    "created_at": str(r["created_at"]),
                    "finished_at": str(r["finished_at"]) if r["finished_at"] else None,
                }
                for r in rows
            ]
            return {"items": items, "total": total, "page": page, "page_size": page_size}
    finally:
        _put_conn(conn)


@router.get("/api/data-import-jobs/{job_id}")
async def get_import_job(job_id: str, request: Request):
    username = get_request_username(request) or "admin"
    conn = _get_conn()
    try:
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT j.*, t.table_name, t.owner_username
                FROM data_import_job j
                JOIN data_table_registry t ON j.table_id = t.id
                WHERE j.id = %s
            """, (job_id,))
            r = cur.fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="任务不存在")
            if not is_admin_user(username) and r["owner_username"] != username:
                raise HTTPException(status_code=403, detail="无权查看此任务")
            return {
                "id": str(r["id"]),
                "table_id": str(r["table_id"]),
                "table_name": r["table_name"],
                "filename": r["filename"],
                "status": r["status"],
                "total_rows": r["total_rows"],
                "success_rows": r["success_rows"],
                "failed_rows": r["failed_rows"],
                "errors": r["errors"],
                "created_at": str(r["created_at"]),
                "finished_at": str(r["finished_at"]) if r["finished_at"] else None,
            }
    finally:
        _put_conn(conn)
```

- [ ] **Step 3: 注册路由**

在 `src/qwenpaw/app/routers/__init__.py` 中添加：
```python
from ...data_tables.import_api import router as data_import_router
```
然后在 `router.include_router(...)` 调用中添加：
```python
router.include_router(data_import_router)
```

- [ ] **Step 4: 验证导入 API 存在**

Run: `docker exec huizhi-v210-check python -c "from qwenpaw.data_tables.import_api import router; print('OK')"`
Expected: OK

- [ ] **Step 5: Commit**

```bash
git add src/qwenpaw/data_tables/importer.py src/qwenpaw/data_tables/import_api.py src/qwenpaw/app/_app.py
git commit -m "feat(data): add async import worker with encoding detection and batch insert"
```

---

## Task 6: 数据预览 API

**Files:**
- Create: `src/qwenpaw/data_tables/preview_api.py`
- Modify: `src/qwenpaw/app/_app.py` — 注册路由

**Interfaces:**
- Produces: `GET /api/data-tables/{id}/preview` — 预览前 N 行数据

- [ ] **Step 1: 创建 preview_api.py**

```python
# src/qwenpaw/data_tables/preview_api.py
"""Data preview API."""

from fastapi import APIRouter, HTTPException, Query, Request

from ..app.user_space import get_request_username, is_admin_user
from .connection import get_target_connection, put_target_connection
from .store import _get_conn, _put_conn

router = APIRouter(tags=["data-preview"])


@router.get("/api/data-tables/{table_id}/preview")
async def preview_table(table_id: str, request: Request, limit: int = Query(100, ge=1, le=500)):
    username = get_request_username(request) or "admin"
    conn = _get_conn()
    try:
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM data_table_registry WHERE id=%s", (table_id,))
            table = cur.fetchone()
            if not table:
                raise HTTPException(status_code=404, detail="表不存在")
            if not is_admin_user(username) and table["owner_username"] != username:
                raise HTTPException(status_code=403, detail="无权查看此表")
    finally:
        _put_conn(conn)
    # Query HighGo
    hg_conn = get_target_connection(table["db_name"])
    try:
        with hg_conn.cursor() as cur:
            cur.execute(f'SELECT * FROM "{table["table_name"]}" LIMIT %s', (limit,))
            col_names = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
        return {
            "columns": col_names,
            "rows": [dict(zip(col_names, row)) for row in rows],
            "total": table["row_count"] or 0,
        }
    finally:
        put_target_connection(hg_conn, table["db_name"])
```

- [ ] **Step 2: 注册路由**

在 `src/qwenpaw/app/routers/__init__.py` 中添加：
```python
from ...data_tables.preview_api import router as data_preview_router
```
然后在 `router.include_router(...)` 调用中添加：
```python
router.include_router(data_preview_router)
```

- [ ] **Step 3: Commit**

```bash
git add src/qwenpaw/data_tables/preview_api.py src/qwenpaw/app/_app.py
git commit -m "feat(data): add table preview API"
```

---

## Task 7: Agent 查询工具

**Files:**
- Create: `src/qwenpaw/agents/tools/data_tables.py`
- Modify: `src/qwenpaw/agents/tools/__init__.py` — 添加 import 行

**Interfaces:**
- Produces: `list_data_tables` / `describe_data_table` / `query_data_table` — 三个 Agent 工具

**Note:** 工具使用 `@tool_descriptor(...)` 装饰器自动注册，无需手动维护工具列表。只需在 `__init__.py` 中添加 import 即可触发自动发现。

- [ ] **Step 1: 创建 data_tables.py 工具**

```python
# src/qwenpaw/agents/tools/data_tables.py
"""Agent tools for data table queries against HighGo."""

import re
import logging
from typing import Any

from ...runtime.tool_registry import tool_descriptor

logger = logging.getLogger(__name__)

_DANGEROUS_SQL_RE = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTASH)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


def _sanitize_sql(sql: str) -> str:
    """Remove comments for security check."""
    s = _BLOCK_COMMENT_RE.sub("", sql)
    s = _LINE_COMMENT_RE.sub("", s)
    return s.strip()


def _is_read_only(sql: str) -> bool:
    """Check if SQL is read-only (SELECT or WITH only)."""
    sanitized = _sanitize_sql(sql)
    if _DANGEROUS_SQL_RE.match(sanitized):
        return False
    first_word = sanitized.split()[0].upper() if sanitized.split() else ""
    return first_word in ("SELECT", "WITH")


def _ensure_limit(sql: str, max_rows: int = 100) -> str:
    """Add LIMIT if missing."""
    sanitized = _sanitize_sql(sql).rstrip(";")
    if "LIMIT" not in sanitized.upper():
        return f"{sanitized} LIMIT {max_rows}"
    return sql


def _resolve_username() -> str:
    """Get current username from ContextVar."""
    from qwenpaw.runtime.contextvars import get_current_user_id
    username = get_current_user_id()
    if username and username != "default":
        return username
    return "admin"


@tool_descriptor(
    name="list_data_tables",
    description="列出当前用户创建的所有数据表名称、描述、列信息和行数。在需要了解有哪些可用数据表时使用。",
    parameters={"type": "object", "properties": {}, "required": []},
)
def list_data_tables() -> str:
    """List all data tables for current user."""
    username = _resolve_username()
    from qwenpaw.data_tables.connection import _slug_username
    from qwenpaw.data_tables.store import _get_conn, _put_conn
    conn = _get_conn()
    try:
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT table_name, description, columns, row_count
                FROM data_table_registry
                WHERE owner_username = %s
                ORDER BY created_at DESC
            """, (username,))
            rows = cur.fetchall()
            if not rows:
                return "当前用户没有已创建的数据表。请先通过「数据分析」菜单创建数据表并导入数据。"
            parts = []
            for r in rows:
                cols_desc = ", ".join(
                    f"{c['name']}({c.get('comment', c['name'])})"
                    for c in (r["columns"] or [])
                )
                parts.append(
                    f"- **{r['table_name']}**：{r['description'] or '无描述'}，"
                    f"{len(r['columns'] or [])} 列，{r['row_count'] or 0} 行。列：{cols_desc}"
                )
            return "当前用户的数据表：\n" + "\n".join(parts)
    finally:
        _put_conn(conn)


@tool_descriptor(
    name="describe_data_table",
    description="查看指定数据表的详细结构：建表语句、列说明（名称、类型、注释）和样例数据。在需要了解某张表的完整结构时使用。",
    parameters={
        "type": "object",
        "properties": {
            "table_name": {
                "type": "string",
                "description": "数据表名称，如 tb_order",
            }
        },
        "required": ["table_name"],
    },
)
def describe_data_table(table_name: str) -> str:
    """Describe a data table's schema, comments, and sample data."""
    username = _resolve_username()
    from qwenpaw.data_tables.store import _get_conn, _put_conn
    from qwenpaw.data_tables.connection import get_target_connection, put_target_connection

    conn = _get_conn()
    try:
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM data_table_registry
                WHERE owner_username = %s AND table_name = %s
            """, (username, table_name))
            table = cur.fetchone()
            if not table:
                return f"表 {table_name} 不存在。请先用 list_data_tables 查看可用表。"
    finally:
        _put_conn(conn)

    columns = table["columns"] or []
    col_defs = []
    pk_cols = [c["name"] for c in columns if c.get("is_primary_key")]
    for col in columns:
        parts = [f'{col["name"]} {col["type"]}']
        if not col.get("nullable", True):
            parts.append("NOT NULL")
        if col.get("comment"):
            parts.append(f"/* {col['comment']} */")
        col_defs.append(" ".join(parts))
    if pk_cols:
        col_defs.append(f"PRIMARY KEY ({', '.join(pk_cols)})")
    ddl = f'CREATE TABLE {table_name} (\n  ' + ",\n  ".join(col_defs) + "\n);"

    col_details = []
    for c in columns:
        col_details.append(f"- {c['name']}（{c['type']}）：{c.get('comment', '无说明')}")

    hg_conn = get_target_connection(table["db_name"])
    try:
        with hg_conn.cursor() as cur:
            cur.execute(f'SELECT * FROM "{table_name}" LIMIT 3')
            col_names = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
        sample_lines = []
        for row in rows:
            sample_lines.append("  " + " | ".join(str(v) for v in row))
        sample = "\n".join(sample_lines) if sample_lines else "（表为空，暂无数据）"
    except Exception as e:
        sample = f"（读取样例数据失败: {e}）"
    finally:
        put_target_connection(hg_conn, table["db_name"])

    return (
        f"## 表 {table_name}\n"
        f"{table['description'] or '无描述'}\n\n"
        f"### 建表语句\n```sql\n{ddl}\n```\n\n"
        f"### 列说明\n" + "\n".join(col_details) + "\n\n"
        f"### 样例数据（前3行）\n```\n{' | '.join(col_names)}\n{sample}\n```"
    )


@tool_descriptor(
    name="query_data_table",
    description="对当前用户的业务数据表执行只读 SQL 查询（仅 SELECT/WITH），返回查询结果。支持聚合分析、分组统计、排序筛选等操作。禁止写入操作。",
    parameters={
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "要执行的只读 SQL 查询语句（仅 SELECT 或 WITH 开头）",
            }
        },
        "required": ["sql"],
    },
)
def query_data_table(sql: str) -> str:
    """Execute a read-only SQL query and return results."""
    if not _is_read_only(sql):
        return "安全限制：仅允许 SELECT 查询。不允许 INSERT/UPDATE/DELETE/DROP 等写入操作。"
    username = _resolve_username()
    sql = _ensure_limit(sql, 100)

    from qwenpaw.data_tables.connection import get_target_connection, put_target_connection
    hg_conn = get_target_connection(username)
    try:
        with hg_conn.cursor() as cur:
            cur.execute("SET statement_timeout = '30s'")
            cur.execute(sql)
            col_names = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
        if not rows:
            return "查询结果为空。"
        lines = [" | ".join(col_names)]
        lines.append("-" * 50)
        for row in rows[:100]:
            lines.append(" | ".join(str(v) for v in row))
        result = "\n".join(lines)
        if len(rows) >= 100:
            result += f"\n\n（结果已截断，共 {len(rows)}+ 行）"
        return result
    except Exception as e:
        return f"查询执行失败: {str(e)}"
    finally:
        put_target_connection(hg_conn, username)
```

- [ ] **Step 2: 注册工具到 __init__.py**

在 `src/qwenpaw/agents/tools/__init__.py` 中添加 import（触发 `@tool_descriptor` 装饰器自动注册）：
```python
from .data_tables import list_data_tables, describe_data_table, query_data_table
```
无需手动维护工具列表，`discover_builtin_tool_funcs()` 会自动扫描。

- [ ] **Step 3: 验证工具导入**

Run: `docker exec huizhi-v210-check python -c "from qwenpaw.agents.tools.data_tables import list_data_tables, describe_data_table, query_data_table; print('OK')"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add src/qwenpaw/agents/tools/data_tables.py src/qwenpaw/agents/tools/__init__.py
git commit -m "feat(data): add Agent tools for data table queries with SQL safety guard"
```

---

## Task 8: 前端「数据分析」页面

**Files:**
- Create: `console/src/pages/DataAnalysis/index.tsx`
- Modify: `console/src/layouts/Sidebar.tsx` — 添加菜单
- Modify: `console/src/layouts/registry/builtinRoutes.tsx` — 添加路由
- Modify: `console/src/pages/Settings/UserManagement/index.tsx` — DEFAULT_MENUS 加 core.data-analysis

**Interfaces:**
- Produces: 数据分析菜单 + 表列表页 + 建表向导 + 导入弹窗 + 预览抽屉

- [ ] **Step 1: 创建数据分析页面（表列表 + 建表 + 导入 + 预览）**

由于前端代码量较大，此步骤需创建 `console/src/pages/DataAnalysis/index.tsx`，包含：
- 表列表（表格组件，分页，模糊查询，时间范围筛选）
- 建表向导（Modal，列编辑器）
- 导入弹窗（Upload 组件，**支持多文件上传**，`multiple` 属性，上传后进入异步任务列表）
- 预览抽屉（Drawer，Schema 视图 + 数据预览）

- [ ] **Step 2: 添加菜单和路由**

在 Sidebar.tsx 的 `ALWAYS_USER_MENU_IDS` 中加入 `"core.data-analysis"`（与 `"core.knowledge"` 同级）。在 builtinRoutes.tsx 中添加 `/data-analysis` 路由指向 DataAnalysis 页面。

- [ ] **Step 3: DEFAULT_MENUS 加入 core.data-analysis**

修改 `console/src/pages/Settings/UserManagement/index.tsx` 的 DEFAULT_MENUS 数组，加入 `"core.data-analysis"`。

- [ ] **Step 4: 构建前端**

Run: `cd console && npm run build`
Expected: 构建成功

- [ ] **Step 5: 部署并验证**

```bash
docker cp console/dist/. huizhi-v210-check:/workspace/app/console/dist/
docker restart huizhi-v210-check
```
访问 `http://localhost:18088/data-analysis` 验证页面加载。

- [ ] **Step 6: Commit**

```bash
git add console/src/pages/DataAnalysis/ console/src/layouts/Sidebar.tsx console/src/layouts/registry/builtinRoutes.tsx console/src/pages/Settings/UserManagement/index.tsx
git commit -m "feat(data): add DataAnalysis frontend page with table list, create wizard, and import"
```

---

## Task 9: 端到端集成验证

- [ ] **Step 1: 配置数据源**

通过 API 或管理端设置瀚高连接（host/port/username/password）。

- [ ] **Step 2: 建表**

在数据分析页面创建表 `tb_order`，添加列：order_id(BIGINT), customer_name(TEXT), order_amount(NUMERIC(12,2)), order_date(DATE), status(INTEGER)。

- [ ] **Step 3: 导入数据**

上传 CSV 文件，验证异步导入任务完成，数据入库。

- [ ] **Step 4: Agent 查询**

在对话框提问"帮我看看订单金额最高的前5个客户"，验证智能体调用工具链并返回自然语言回答。

- [ ] **Step 5: Commit 所有变更**

```bash
git add -A
git commit -m "feat(data): data analysis module - first phase complete"
```

---

## 文件结构总结

```
src/qwenpaw/
├── data_tables/
│   ├── __init__.py
│   ├── store.py          # 系统PG17元数据存储
│   ├── connection.py     # 瀚高连接管理+建库隔离
│   ├── config_api.py     # 数据源配置API
│   ├── table_api.py      # 表管理API
│   ├── import_api.py     # 导入任务API
│   ├── preview_api.py    # 数据预览API
│   └── importer.py       # 异步导入Worker
├── agents/tools/
│   └── data_tables.py    # Agent查询工具
console/src/pages/
├── DataAnalysis/
│   └── index.tsx         # 数据分析前端页面
```
