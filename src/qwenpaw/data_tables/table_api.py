"""Data table management API."""

import json
import re

import psycopg2.extras
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..app.user_space import get_request_username, is_admin_user
from .connection import _slug_username, drop_table, generate_table_name
from .store import _get_conn, _put_conn

router = APIRouter(prefix="/data-tables", tags=["data-tables"])

TABLE_NAME_RE = re.compile(r"^tb_[a-z][a-z0-9_]{0,62}$")


class ColumnNote(BaseModel):
    """A user-provided Chinese description of a data column."""

    comment: str


class CreateTableRequest(BaseModel):
    display_name: str = ""
    description: str = ""
    columns: list[str] = Field(default_factory=list)


@router.get("", response_model=dict)
async def list_tables(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: str | None = Query(None),
    created_start: str | None = Query(None),
    created_end: str | None = Query(None),
):
    username = get_request_username(request) or "admin"
    is_admin = is_admin_user(username)
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = []
            params: list = []
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
            cur.execute(f"SELECT COUNT(*) as cnt FROM data_table_registry{where}", params)
            total = cur.fetchone()["cnt"]
            offset = (page - 1) * page_size
            cur.execute(
                f"SELECT * FROM data_table_registry{where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
                params + [page_size, offset],
            )
            rows = cur.fetchall()
            items = [
                {
                    "id": str(r["id"]),
                    "table_name": r["table_name"],
                    "display_name": r.get("display_name") or "",
                    "description": r["description"] or "",
                    "column_count": len(r["columns"]) if r["columns"] else 0,
                    "row_count": r["row_count"] or 0,
                    "columns": r["columns"] or [],
                    "db_name": r["db_name"],
                    "initialized": r["initialized"],
                    "created_at": str(r["created_at"]),
                    "updated_at": str(r["updated_at"]),
                }
                for r in rows
            ]
            return {"items": items, "total": total, "page": page, "page_size": page_size}
    finally:
        _put_conn(conn)


@router.post("", status_code=201)
async def create_table(payload: CreateTableRequest, request: Request):
    """Create a column-mapping definition. Real DB table is created lazily on first import."""
    username = get_request_username(request) or "admin"
    if not payload.display_name.strip():
        raise HTTPException(status_code=400, detail="表名不能为空")
    if not payload.description.strip():
        raise HTTPException(status_code=400, detail="表描述不能为空")
    if not payload.columns or not any(c.strip() for c in payload.columns):
        raise HTTPException(status_code=400, detail="请至少描述一列的含义")
    for i, col in enumerate(payload.columns, 1):
        if not col.strip():
            raise HTTPException(status_code=400, detail=f"第 {i} 列的含义不能为空，请填写后再提交")

    # Always auto-generate table_name
    table_name = generate_table_name("", payload.description or payload.display_name)

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM data_table_registry WHERE owner_username=%s AND table_name=%s",
                (username, table_name),
            )
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="表名已存在")
    finally:
        _put_conn(conn)

    cols = [{"comment": c.strip(), "column_name": None, "type": None, "nullable": True, "is_primary_key": False} for c in payload.columns if c.strip()]
    db_name = _slug_username(username)
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO data_table_registry (owner_username, db_name, table_name, display_name, description, columns)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """,
                (username, "", table_name, payload.display_name.strip(), payload.description.strip(), json.dumps(cols)),
            )
            row_id = cur.fetchone()[0]
            conn.commit()
        return {"id": str(row_id), "table_name": table_name}
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)


@router.get("/{table_id}")
async def get_table(table_id: str, request: Request):
    username = get_request_username(request) or "admin"
    is_adm = is_admin_user(username)
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM data_table_registry WHERE id = %s", (table_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="表不存在")
            if not is_adm and row["owner_username"] != username:
                raise HTTPException(status_code=403, detail="无权查看此表")
            cols = row["columns"] or []
            # Count import jobs
            cur.execute(
                "SELECT COUNT(*) as cnt, COALESCE(SUM(success_rows),0) as total_imported FROM data_import_job WHERE table_id=%s",
                (table_id,),
            )
            job_stats = cur.fetchone()
            return {
                "id": str(row["id"]),
                "table_name": row["table_name"],
                "display_name": row.get("display_name") or "",
                "description": row["description"] or "",
                "columns": cols,
                "column_count": len(cols),
                "row_count": row["row_count"] or 0,
                "db_name": row["db_name"],
                "initialized": row["initialized"],
                "import_count": job_stats["cnt"] if job_stats else 0,
                "total_imported": int(job_stats["total_imported"]) if job_stats else 0,
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
    finally:
        _put_conn(conn)


@router.delete("/{table_id}")
async def delete_table(table_id: str, request: Request):
    username = get_request_username(request) or "admin"
    is_adm = is_admin_user(username)
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM data_table_registry WHERE id = %s", (table_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="表不存在")
            if not is_adm and row["owner_username"] != username:
                raise HTTPException(status_code=403, detail="无权删除此表")
            if row["initialized"]:
                drop_table(row["db_name"], row["table_name"])
            cur.execute("DELETE FROM data_table_registry WHERE id = %s", (table_id,))
            conn.commit()
        return {"ok": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)


@router.post("/batch-delete")
async def batch_delete_tables(payload: dict, request: Request):
    ids = payload.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="请选择要删除的数据表")
    username = get_request_username(request) or "admin"
    is_adm = is_admin_user(username)
    conn = _get_conn()
    deleted = 0
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for tid in ids:
                cur.execute("SELECT * FROM data_table_registry WHERE id = %s", (tid,))
                row = cur.fetchone()
                if not row:
                    continue
                if not is_adm and row["owner_username"] != username:
                    continue
                if row["initialized"]:
                    try:
                        drop_table(row["db_name"], row["table_name"])
                    except Exception:
                        pass
                cur.execute("DELETE FROM data_table_registry WHERE id = %s", (tid,))
                deleted += 1
            conn.commit()
        return {"ok": True, "deleted": deleted}
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)
