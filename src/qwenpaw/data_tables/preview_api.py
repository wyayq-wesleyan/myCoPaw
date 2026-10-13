"""Data preview API."""

import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..app.user_space import get_request_username
from ..utils.io_utils import run_sync_io
from .connection import get_target_connection, put_target_connection
from .store import (
    SYSTEM_TABLE_REGISTRY,
    _get_conn,
    _put_conn,
    init_data_tables_schema,
    schema_for_registry_row,
)

router = APIRouter(tags=["data-preview"], dependencies=[Depends(init_data_tables_schema)])


def _preview_table_sync(table_id: str, username: str, limit: int) -> dict:
    """Load one table's preview. Blocking: dispatched to a worker thread.

    ``psycopg2`` is synchronous, and ``SELECT *`` reads rows, so running this on
    the event loop would stall every other request — including other accounts'
    chat turns — for the whole query.
    """
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM {SYSTEM_TABLE_REGISTRY} WHERE id=%s", (table_id,))
            table = cur.fetchone()
    finally:
        _put_conn(conn)
    if not table:
        raise HTTPException(status_code=404, detail="表不存在")
    if table["owner_username"] != username:
        raise HTTPException(status_code=403, detail="无权查看此表")
    if not table["initialized"]:
        return {"columns": [], "rows": [], "total": 0, "initialized": False}

    # The table's schema follows its path: a table filed into a case lives in that
    # path's schema, so opening the connection on ``public`` would fail with
    # "relation does not exist" for every filed table. The same schema must be
    # passed back on return — the pool is keyed by ``(db_name, schema)``.
    table_schema = schema_for_registry_row(table)
    hg_conn = get_target_connection(table["db_name"], table_schema)
    try:
        with hg_conn.cursor() as cur:
            cur.execute(f'SELECT * FROM "{table["table_name"]}" LIMIT %s', (limit,))
            col_names = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
        # Match descriptions to physical columns **by name**, never by position.
        # The real table carries the ``id`` primary key first, which the registry
        # does not describe, so zipping the two lists by index shifted every
        # label by one: the ``id`` column showed the first user description, each
        # later label borrowed its neighbour's, and the last column lost its
        # label entirely. The registry only records a name after the first
        # import, so an unknown name has no description rather than borrowing one.
        described = {
            str(col.get("column_name")): col
            for col in (table["columns"] or [])
            if col.get("column_name")
        }
        column_meta = []
        for name in col_names:
            meta = described.get(name) or {}
            column_meta.append(
                {
                    "name": name,
                    "comment": meta.get("comment") or "",
                    "type": meta.get("type") or "",
                    "is_primary_key": name == "id",
                },
            )
        return {
            "columns": col_names,
            "column_meta": column_meta,
            "rows": [dict(zip(col_names, row)) for row in rows],
            "total": table["row_count"] or 0,
            "initialized": True,
        }
    finally:
        put_target_connection(hg_conn, table["db_name"], table_schema)


@router.get("/data-tables/{table_id}/preview")
async def preview_table(table_id: str, request: Request, limit: int = Query(100, ge=1, le=500)):
    username = get_request_username(request) or "admin"
    return await run_sync_io(_preview_table_sync, table_id, username, limit)
