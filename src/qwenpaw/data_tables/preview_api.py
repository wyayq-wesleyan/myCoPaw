"""Data preview API."""

import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..app.user_space import get_request_username
from .connection import get_target_connection, put_target_connection
from .store import SYSTEM_TABLE_REGISTRY, _get_conn, _put_conn, init_data_tables_schema

router = APIRouter(tags=["data-preview"], dependencies=[Depends(init_data_tables_schema)])


@router.get("/data-tables/{table_id}/preview")
async def preview_table(table_id: str, request: Request, limit: int = Query(100, ge=1, le=500)):
    username = get_request_username(request) or "admin"
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM {SYSTEM_TABLE_REGISTRY} WHERE id=%s", (table_id,))
            table = cur.fetchone()
            if not table:
                raise HTTPException(status_code=404, detail="表不存在")
            if table["owner_username"] != username:
                raise HTTPException(status_code=403, detail="无权查看此表")
    finally:
        _put_conn(conn)
    if not table["initialized"]:
        return {"columns": [], "rows": [], "total": 0, "initialized": False}
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
            "initialized": True,
        }
    finally:
        put_target_connection(hg_conn, table["db_name"])
