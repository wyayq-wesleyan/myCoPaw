"""Data import API — file upload and job management."""

import os
import uuid

import psycopg2.extras
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile

from ..app.user_space import get_request_username, is_admin_user
from .importer import start_import_task
from .store import _get_conn, _put_conn

router = APIRouter(tags=["data-import"])

UPLOAD_DIR = "/app/working/data_uploads"
ALLOWED_EXTENSIONS = {".csv", ".xls", ".xlsx"}


@router.post("/data-tables/{table_id}/import")
async def import_data(table_id: str, files: list[UploadFile] = File(...), request: Request = None):
    username = get_request_username(request) or "admin"
    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个文件")
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM data_table_registry WHERE id=%s", (table_id,))
            table = cur.fetchone()
            if not table:
                raise HTTPException(status_code=404, detail="表不存在")
            if not is_admin_user(username) and table["owner_username"] != username:
                raise HTTPException(status_code=403, detail="无权操作此表")
    finally:
        _put_conn(conn)
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


@router.get("/data-import-jobs")
async def list_import_jobs(
    request: Request,
    table_id: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50),
):
    username = get_request_username(request) or "admin"
    is_adm = is_admin_user(username)
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
            if not is_adm:
                conditions.append("t.owner_username = %s")
                params.append(username)
            where = ""
            if conditions:
                where = " WHERE " + " AND ".join(conditions)
            cur.execute(
                f"""
                SELECT COUNT(*) as cnt
                FROM data_import_job j
                JOIN data_table_registry t ON j.table_id = t.id
                {where}
            """,
                params,
            )
            total = cur.fetchone()["cnt"]
            offset = (page - 1) * page_size
            cur.execute(
                f"""
                SELECT j.*, t.table_name
                FROM data_import_job j
                JOIN data_table_registry t ON j.table_id = t.id
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


@router.get("/data-import-jobs/{job_id}")
async def get_import_job(job_id: str, request: Request):
    username = get_request_username(request) or "admin"
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT j.*, t.table_name, t.owner_username
                FROM data_import_job j
                JOIN data_table_registry t ON j.table_id = t.id
                WHERE j.id = %s
            """,
                (job_id,),
            )
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
