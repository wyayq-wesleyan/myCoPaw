"""Data import API — file upload and job management.

These endpoints are plain ``def`` rather than ``async def`` on purpose: they
call ``psycopg2``, which is synchronous, so FastAPI runs them in its worker
threadpool rather than on the event loop, where a slow query would stall every
other request.
"""


import os
import json
import uuid
from pathlib import Path

import psycopg2.extras
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse

from ..app.user_space import get_request_username
from ..utils.io_utils import run_sync_io
from .importer import start_import_task
from .store import (
    ALLOWED_EXTENSIONS,
    MAX_IMPORT_FILE_BYTES,
    MAX_XLS_FILE_BYTES,
    SYSTEM_TABLE_IMPORT_JOB,
    SYSTEM_TABLE_REGISTRY,
    _get_conn,
    _put_conn,
    init_data_tables_schema,
    user_upload_dir,
)

router = APIRouter(tags=["data-import"], dependencies=[Depends(init_data_tables_schema)])

UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024


async def _save_upload_file(file: UploadFile, destination: Path) -> int:
    """Stream an import file to disk so multi-GB uploads never sit in memory."""
    size = 0
    try:
        with destination.open("wb") as output:
            while chunk := await file.read(UPLOAD_CHUNK_BYTES):
                size += len(chunk)
                if size > MAX_IMPORT_FILE_BYTES:
                    raise HTTPException(status_code=413, detail="数据分析单文件不能超过 10GB")
                output.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return size


def _assert_table_writable(table_id: str, username: str) -> None:
    """Blocking ownership check. Call it off the event loop."""
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM {SYSTEM_TABLE_REGISTRY} WHERE id=%s", (table_id,))
            table = cur.fetchone()
            if not table:
                raise HTTPException(status_code=404, detail="表不存在")
            if table["owner_username"] != username:
                raise HTTPException(status_code=403, detail="无权操作此表")
    finally:
        _put_conn(conn)


@router.post("/data-tables/{table_id}/import")
async def import_data(table_id: str, files: list[UploadFile] = File(...), request: Request = None):
    username = get_request_username(request) or "admin"
    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个文件")
    # This handler must stay ``async`` because reading the uploads is awaited,
    # so the synchronous ownership query is dispatched explicitly instead of
    # blocking the event loop for every other account.
    await run_sync_io(_assert_table_writable, table_id, username)
    job_ids = []
    errors = []
    for file in files:
        ext = os.path.splitext(file.filename or "")[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            errors.append({"filename": file.filename, "error": f"不支持的文件格式: {ext}"})
            continue
        task_id = str(uuid.uuid4())
        task_dir = os.path.join(user_upload_dir(username), task_id)
        os.makedirs(task_dir, exist_ok=True)
        file_path = os.path.join(task_dir, file.filename or "data.csv")
        file_size = await _save_upload_file(file, Path(file_path))
        if ext == ".xls" and file_size > MAX_XLS_FILE_BYTES:
            Path(file_path).unlink(missing_ok=True)
            errors.append({
                "filename": file.filename,
                "error": "传统 XLS 文件不能超过 512MB，请转换为 CSV 或 XLSX",
            })
            continue
        job_id = await start_import_task(table_id, file_path, file.filename or "data.csv")
        job_ids.append({"job_id": job_id, "filename": file.filename})
    return {"jobs": job_ids, "errors": errors}


@router.get("/data-import-jobs")
def list_import_jobs(
    request: Request,
    table_id: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50),
):
    username = get_request_username(request) or "admin"
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = []
            params: list = []
            if table_id:
                conditions.append("j.table_id = %s")
                params.append(table_id)
            if status:
                conditions.append("j.status = %s")
                params.append(status)
            # Strictly per account, administrators included.
            conditions.append("t.owner_username = %s")
            params.append(username)
            where = ""
            if conditions:
                where = " WHERE " + " AND ".join(conditions)
            cur.execute(
                f"""
                SELECT COUNT(*) as cnt
                FROM {SYSTEM_TABLE_IMPORT_JOB} j
                JOIN {SYSTEM_TABLE_REGISTRY} t ON j.table_id = t.id
                {where}
            """,
                params,
            )
            total = cur.fetchone()["cnt"]
            offset = (page - 1) * page_size
            cur.execute(
                f"""
                SELECT j.*, t.table_name
                FROM {SYSTEM_TABLE_IMPORT_JOB} j
                JOIN {SYSTEM_TABLE_REGISTRY} t ON j.table_id = t.id
                {where}
                ORDER BY j.created_at DESC
                LIMIT %s OFFSET %s
            """,
                params + [page_size, offset],
            )
            rows = cur.fetchall()
            items = [
                {
                    "id": str(r["id"]),
                    "table_id": str(r["table_id"]),
                    "table_name": r["table_name"],
                    "filename": r["filename"],
                    "status": r["status"],
                    "attempts": r.get("attempts", 0),
                    "next_retry_at": str(r["next_retry_at"]) if r.get("next_retry_at") else None,
                    "total_rows": r["total_rows"],
                    "success_rows": r["success_rows"],
                    "failed_rows": r["failed_rows"],
                    "errors": r["errors"],
                    "created_at": str(r["created_at"]),
                    "started_at": str(r["started_at"]) if r["started_at"] else None,
                    "finished_at": str(r["finished_at"]) if r["finished_at"] else None,
                    "processing_duration": (
                        (r["finished_at"] - r["started_at"]).total_seconds()
                        if r["started_at"] and r["finished_at"] else None
                    ),
                }
                for r in rows
            ]
            return {"items": items, "total": total, "page": page, "page_size": page_size}
    finally:
        _put_conn(conn)


@router.get("/data-import-jobs/{job_id}")
def get_import_job(job_id: str, request: Request):
    username = get_request_username(request) or "admin"
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT j.*, t.table_name, t.owner_username
                FROM {SYSTEM_TABLE_IMPORT_JOB} j
                JOIN {SYSTEM_TABLE_REGISTRY} t ON j.table_id = t.id
                WHERE j.id = %s
            """,
                (job_id,),
            )
            r = cur.fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="任务不存在")
            if r["owner_username"] != username:
                raise HTTPException(status_code=403, detail="无权查看此任务")
            return {
                "id": str(r["id"]),
                "table_id": str(r["table_id"]),
                "table_name": r["table_name"],
                "filename": r["filename"],
                "status": r["status"],
                "attempts": r.get("attempts", 0),
                "next_retry_at": str(r["next_retry_at"]) if r.get("next_retry_at") else None,
                "total_rows": r["total_rows"],
                "success_rows": r["success_rows"],
                "failed_rows": r["failed_rows"],
                "errors": r["errors"],
                "created_at": str(r["created_at"]),
                "started_at": str(r["started_at"]) if r["started_at"] else None,
                "finished_at": str(r["finished_at"]) if r["finished_at"] else None,
                "processing_duration": (
                    (r["finished_at"] - r["started_at"]).total_seconds()
                    if r["started_at"] and r["finished_at"] else None
                ),
            }
    finally:
        _put_conn(conn)


@router.get("/data-import-jobs/{job_id}/log")
def get_import_job_log(job_id: str, request: Request):
    username = get_request_username(request) or "admin"
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""SELECT j.log_excerpt, j.errors, t.owner_username
                    FROM {SYSTEM_TABLE_IMPORT_JOB} j JOIN {SYSTEM_TABLE_REGISTRY} t ON t.id=j.table_id
                    WHERE j.id=%s""", (job_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="任务不存在")
            if row["owner_username"] != username:
                raise HTTPException(status_code=403, detail="无权查看此任务")
            text = row.get("log_excerpt") or json.dumps(row.get("errors") or [], ensure_ascii=False, indent=2)
            return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="import-{job_id}.log"'})
    finally:
        _put_conn(conn)
