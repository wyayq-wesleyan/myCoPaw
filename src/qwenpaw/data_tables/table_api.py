"""Data table management API.

These endpoints are plain ``def`` rather than ``async def`` on purpose: they
call ``psycopg2``, which is synchronous, so FastAPI runs them in its worker
threadpool instead of on the event loop. Making one ``async`` without also
dispatching the query would let a single slow statement (a scan over a large
table) stall every other request, including other accounts' chat turns.
"""

import json
import re

import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..app.user_space import get_request_username
from .connection import drop_table, ensure_user_database, generate_table_name
from .store import (
    ALLOWED_COLUMN_TYPES,
    COLUMN_TYPE_TEXT,
    SYSTEM_TABLE_IMPORT_JOB,
    SYSTEM_TABLE_REGISTRY,
    _get_conn,
    _put_conn,
    init_data_tables_schema,
)

router = APIRouter(
    prefix="/data-tables",
    tags=["data-tables"],
    dependencies=[Depends(init_data_tables_schema)],
)

TABLE_NAME_RE = re.compile(r"^tb_[a-z][a-z0-9_]{0,62}$")


class ColumnNote(BaseModel):
    """A user-provided Chinese description of a data column."""

    comment: str


class CreateTableRequest(BaseModel):
    display_name: str = ""
    description: str = ""
    columns: list[str] = Field(default_factory=list)
    # Optional per-column type keys (see ALLOWED_COLUMN_TYPES). Positional, and
    # a missing/short list means every column defaults to TEXT.
    column_types: list[str] = Field(default_factory=list)


class UpdateTableRequest(BaseModel):
    """Editable table metadata.

    ``table_name`` is the physical name and is deliberately NOT editable: it is
    already referenced by the account's real database table, so renaming it here
    would orphan the data. Only the labels shown in the console change.
    """

    display_name: str | None = None
    description: str | None = None


def create_table_record(
    username: str,
    display_name: str = "",
    description: str = "",
    columns: list[str] | None = None,
    column_types: list[str] | None = None,
    path_id: str = "",
) -> dict[str, str]:
    """Create one account's table definition and return its id and table name.

    The console API requires the user to describe every column meaning; the
    agent tool may pass none, because the real column names are inferred from
    the imported file's header row when the physical table is created on first
    import. Column types default to TEXT; a caller may pin them per column.
    """
    description = (description or "").strip()
    display_name = (display_name or "").strip()

    # Always auto-generate table_name
    table_name = generate_table_name("", description or display_name)
    db_name = ensure_user_database(username)

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT 1 FROM {SYSTEM_TABLE_REGISTRY} WHERE owner_username=%s AND table_name=%s",
                (username, table_name),
            )
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="表名已存在")
    finally:
        _put_conn(conn)

    requested = list(column_types or [])
    for index, value in enumerate(requested):
        key = str(value or "").strip().upper()
        if key and key not in ALLOWED_COLUMN_TYPES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"第 {index + 1} 列的数据类型不支持：{value}。"
                    f"可选：{', '.join(ALLOWED_COLUMN_TYPES)}"
                ),
            )

    cols = []
    for index, raw in enumerate(columns or []):
        comment = str(raw).strip()
        if not comment:
            continue
        requested_type = (
            str(requested[index]).strip().upper()
            if index < len(requested) and str(requested[index] or "").strip()
            else COLUMN_TYPE_TEXT
        )
        cols.append(
            {
                "comment": comment,
                "column_name": None,
                # TEXT is stored explicitly: the importer reads this field, and
                # an explicit value is what keeps identifiers from being
                # inferred into an INTEGER that cannot hold them.
                "type": requested_type,
                "nullable": True,
                "is_primary_key": False,
            },
        )
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {SYSTEM_TABLE_REGISTRY} (owner_username, db_name, table_name, display_name, description, columns, path_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """,
                (
                    username, db_name, table_name, display_name, description,
                    json.dumps(cols),
                    # Mirrors the binding below for readability of the raw row; it
                    # is NOT the authority (see ``schema_for_registry_row``).
                    str(path_id or ""),
                ),
            )
            row_id = cur.fetchone()[0]
            conn.commit()

        # The authoritative membership record. Written outside the registry
        # transaction because it lives in the platform store, and a binding
        # failure must surface rather than leave a table that is physically in one
        # schema and logically in another.
        if path_id:
            from ..platform_store import paths as path_store

            path_store.bind_resource(
                path_id, path_store.KIND_DATA_TABLE, str(row_id),
            )
        return {"id": str(row_id), "table_name": table_name}
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)


def find_owned_table(username: str, table_ref: str) -> dict | None:
    """Resolve a table by id or name, scoped strictly to one account.

    Unlike the console endpoints this never falls back to an admin override:
    a chat tool must only ever reach the caller's own tables.
    """
    table_ref = (table_ref or "").strip()
    if not table_ref:
        return None
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT * FROM {SYSTEM_TABLE_REGISTRY}
                WHERE owner_username = %s AND (table_name = %s OR id::text = %s)
            """,
                (username, table_ref, table_ref),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        _put_conn(conn)


@router.get("", response_model=dict)
def list_tables(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: str | None = Query(None),
    created_start: str | None = Query(None),
    created_end: str | None = Query(None),
    path_id: str | None = Query(None),
):
    """List the account's data tables, optionally narrowed to one path.

    ``path_id`` filters by *the path the table is filed under*, computed from the
    path model rather than from a column comparison, because an unfiled table
    belongs to the account's public area (root) — the same rule the query tools
    use. An unknown or foreign path yields an empty page, never the unfiltered
    list.
    """
    username = get_request_username(request) or "admin"
    path_filter: set[str] | None = None
    if path_id:
        from ..platform_store import paths as path_store

        # The account's full table id set, so the store can work out which are
        # unfiled (public). Fail closed: an unresolvable path must not fall back
        # to the unfiltered list.
        conn_ids = _get_conn()
        try:
            with conn_ids.cursor() as cur:
                cur.execute(
                    f"SELECT id FROM {SYSTEM_TABLE_REGISTRY} WHERE owner_username=%s",
                    (username,),
                )
                all_table_ids = [str(row[0]) for row in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"数据表列表读取失败：{exc}") from exc
        finally:
            _put_conn(conn_ids)

        try:
            visible = path_store.effective_table_ids_for_path(
                path_id, username, all_table_ids,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed, not open
            raise HTTPException(status_code=503, detail=f"路径解析失败：{exc}") from exc
        if visible is None:
            return {"items": [], "total": 0, "page": page, "page_size": page_size}
        path_filter = set(visible)

    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = []
            params: list = []
            # Strictly per account, including for administrators: data tables
            # follow the same rule as knowledge spaces and agent ownership.
            # The admin bypass that used to be here was one of three
            # inconsistent admin semantics in the codebase.
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
            if path_filter is not None:
                if not path_filter:
                    # The path exists but holds nothing: return an empty page
                    # rather than skipping the filter.
                    return {"items": [], "total": 0, "page": page, "page_size": page_size}
                conditions.append("id = ANY(%s)")
                params.append(sorted(path_filter))
            where = " WHERE " + " AND ".join(conditions) if conditions else ""
            cur.execute(f"SELECT COUNT(*) as cnt FROM {SYSTEM_TABLE_REGISTRY}{where}", params)
            total = cur.fetchone()["cnt"]
            offset = (page - 1) * page_size
            cur.execute(
                f"SELECT * FROM {SYSTEM_TABLE_REGISTRY}{where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
                params + [page_size, offset],
            )
            rows = cur.fetchall()
            pending_counts: dict[str, int] = {}
            for row in rows:
                cur.execute(
                    f"SELECT COUNT(*) AS cnt FROM {SYSTEM_TABLE_IMPORT_JOB} WHERE table_id=%s AND status IN ('pending','parsing','loading')",
                    (row["id"],),
                )
                pending_counts[str(row["id"])] = int(cur.fetchone()["cnt"] or 0)
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
                    "pending_import_count": pending_counts.get(str(r["id"]), 0),
                }
                for r in rows
            ]
            return {"items": items, "total": total, "page": page, "page_size": page_size}
    finally:
        _put_conn(conn)


@router.post("", status_code=201)
def create_table(payload: CreateTableRequest, request: Request):
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

    return create_table_record(
        username,
        display_name=payload.display_name,
        description=payload.description,
        columns=payload.columns,
        column_types=payload.column_types,
    )


@router.get("/{table_id}")
def get_table(table_id: str, request: Request):
    username = get_request_username(request) or "admin"
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM {SYSTEM_TABLE_REGISTRY} WHERE id = %s", (table_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="表不存在")
            if row["owner_username"] != username:
                raise HTTPException(status_code=403, detail="无权查看此表")
            cols = row["columns"] or []
            # Count import jobs
            cur.execute(
                f"SELECT COUNT(*) as cnt, COALESCE(SUM(success_rows),0) as total_imported FROM {SYSTEM_TABLE_IMPORT_JOB} WHERE table_id=%s",
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


@router.put("/{table_id}")
def update_table(table_id: str, payload: UpdateTableRequest, request: Request):
    """Update a table's display name and description.

    Ownership is checked the same way as every other endpoint; there is no
    admin override. The physical table name and the imported data are untouched.
    """
    username = get_request_username(request) or "admin"

    updates: dict[str, str] = {}
    if payload.display_name is not None:
        display_name = payload.display_name.strip()
        if not display_name:
            raise HTTPException(status_code=400, detail="表名不能为空")
        updates["display_name"] = display_name
    if payload.description is not None:
        description = payload.description.strip()
        if not description:
            raise HTTPException(status_code=400, detail="表描述不能为空")
        updates["description"] = description
    if not updates:
        raise HTTPException(status_code=400, detail="没有需要修改的内容")

    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM {SYSTEM_TABLE_REGISTRY} WHERE id = %s", (table_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="表不存在")
            if row["owner_username"] != username:
                raise HTTPException(status_code=403, detail="无权修改此表")

            assignments = ", ".join(f"{field} = %s" for field in updates)
            cur.execute(
                f"UPDATE {SYSTEM_TABLE_REGISTRY} SET {assignments}, updated_at = now() WHERE id = %s",
                [*updates.values(), table_id],
            )
            conn.commit()
        return {"ok": True, **updates}
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)


@router.delete("/{table_id}")
def delete_table(table_id: str, request: Request):
    username = get_request_username(request) or "admin"
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM {SYSTEM_TABLE_REGISTRY} WHERE id = %s", (table_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="表不存在")
            if row["owner_username"] != username:
                raise HTTPException(status_code=403, detail="无权删除此表")
            if row["initialized"]:
                drop_table(row["db_name"], row["table_name"])
            cur.execute(f"DELETE FROM {SYSTEM_TABLE_REGISTRY} WHERE id = %s", (table_id,))
            conn.commit()
        return {"ok": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)


@router.post("/batch-delete")
def batch_delete_tables(payload: dict, request: Request):
    ids = payload.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="请选择要删除的数据表")
    username = get_request_username(request) or "admin"
    conn = _get_conn()
    deleted = 0
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for tid in ids:
                cur.execute(f"SELECT * FROM {SYSTEM_TABLE_REGISTRY} WHERE id = %s", (tid,))
                row = cur.fetchone()
                if not row:
                    continue
                if row["owner_username"] != username:
                    continue
                if row["initialized"]:
                    try:
                        drop_table(row["db_name"], row["table_name"])
                    except Exception:
                        pass
                cur.execute(f"DELETE FROM {SYSTEM_TABLE_REGISTRY} WHERE id = %s", (tid,))
                deleted += 1
            conn.commit()
        return {"ok": True, "deleted": deleted}
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)
