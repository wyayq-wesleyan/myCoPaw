"""Data source configuration API for admin settings."""

import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..app.auth import require_admin_access
from ..app.user_space import get_request_username
from .store import SYSTEM_TABLE_SOURCE_CONFIG, _get_conn, _put_conn, init_data_tables_schema

router = APIRouter(
    prefix="/data-source",
    tags=["data-source"],
    dependencies=[Depends(init_data_tables_schema)],
)


class DataSourceConfig(BaseModel):
    name: str = Field(default="highgo_default", pattern=r"^[a-z][a-z0-9_]{0,31}$")
    host: str
    port: int = Field(default=5432, ge=1, le=65535)
    username: str
    password: str


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
            cur.execute(f"SELECT * FROM {SYSTEM_TABLE_SOURCE_CONFIG} ORDER BY created_at DESC LIMIT 1")
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
            cur.execute(
                """
                INSERT INTO {SYSTEM_TABLE_SOURCE_CONFIG} (name, host, port, username, password_encrypted, updated_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (name) DO UPDATE SET
                    host=EXCLUDED.host, port=EXCLUDED.port,
                    username=EXCLUDED.username, password_encrypted=EXCLUDED.password_encrypted,
                    updated_at=now()
            """,
                (payload.name, payload.host, payload.port, payload.username, payload.password),
            )
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
            cur.execute(f"SELECT * FROM {SYSTEM_TABLE_SOURCE_CONFIG} ORDER BY created_at DESC LIMIT 1")
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="数据源未配置")
    finally:
        _put_conn(conn)
    import psycopg2 as pg2

    try:
        test_conn = pg2.connect(
            host=row["host"],
            port=row["port"],
            user=row["username"],
            password=row["password_encrypted"],
            connect_timeout=5,
        )
        test_conn.close()
        return {"ok": True, "message": "连接成功"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"连接失败: {str(e)}")
