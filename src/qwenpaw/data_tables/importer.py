"""Bounded background data importer for the multi-user application."""

import asyncio
import json
import logging
import os
import re
import traceback
import threading
import uuid
from datetime import datetime, timedelta, timezone
from itertools import chain
from pathlib import Path
from typing import Any, Iterator

import pandas as pd
import psycopg2
import psycopg2.extras

from .connection import (
    ensure_user_database,
    get_target_connection,
    put_target_connection,
    sanitize_column_name,
)
from .store import (
    ALLOWED_COLUMN_TYPES,
    COLUMN_TYPE_AUTO,
    SYSTEM_TABLE_IMPORT_JOB,
    SYSTEM_TABLE_REGISTRY,
    _get_conn,
    _put_conn,
    cleanup_staged_file,
    ensure_path_schema,
    schema_for_registry_row as _schema_for_registry_row,
)

logger = logging.getLogger(__name__)

DATE_FORMATS = ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%d/%m/%Y", "%m/%d/%Y", "%Y年%m月%d日"]
TIMESTAMP_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y%m%d %H:%M:%S",
    "%Y年%m月%d日 %H:%M:%S",
]
IMPORT_CHUNK_ROWS = 10_000

# Keep CPU-heavy Pandas parsing and synchronous database writes away from the
# FastAPI event loop.  A small global limit prevents several users from
# starting large imports at the same time and exhausting the host.
_IMPORT_MAX_CONCURRENCY = max(
    1,
    int(os.getenv("QWENPAW_DATA_IMPORT_MAX_CONCURRENCY", "2")),
)
_import_semaphore: asyncio.Semaphore | None = None
_scheduled_jobs: set[str] = set()

# Serializes the first-import "decide the schema and CREATE TABLE" step per
# table. Import jobs run in worker threads (``asyncio.to_thread``), so a
# ``threading.Lock`` is required — an asyncio lock would not span them.
#
# Without it, dropping several files onto a brand-new table makes every job read
# ``initialized=false`` and then all of them run ``CREATE TABLE`` concurrently.
# The losers fail with a duplicate-object error on the table's row type
# (``pg_type_typname_nsp_index``) even though the import itself is valid. Whoever
# wins the lock initializes the table; the rest fall through to the branch that
# reloads the persisted schema.
_init_locks: dict[str, threading.Lock] = {}
_init_locks_guard = threading.Lock()


def _init_lock_for(table_id: str) -> threading.Lock:
    """Return the creation lock for one table, creating it on first use."""
    with _init_locks_guard:
        lock = _init_locks.get(table_id)
        if lock is None:
            lock = threading.Lock()
            _init_locks[table_id] = lock
        return lock


def _get_import_semaphore() -> asyncio.Semaphore:
    global _import_semaphore
    if _import_semaphore is None:
        _import_semaphore = asyncio.Semaphore(_IMPORT_MAX_CONCURRENCY)
    return _import_semaphore


def _detect_encoding(file_path: str) -> str:
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
    return "utf-8"


def _read_df(file_path: str, filename: str) -> pd.DataFrame:
    """Read a legacy XLS file. CSV/XLSX imports use _iter_data_frames instead."""
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".csv":
        enc = _detect_encoding(file_path)
        return pd.read_csv(file_path, encoding=enc, dtype=str, keep_default_na=False)
    elif ext in (".xls", ".xlsx"):
        return pd.read_excel(file_path, dtype=str, keep_default_na=False)
    else:
        raise ValueError(f"不支持的文件格式: {ext}")


def _iter_xlsx_frames(file_path: str) -> Iterator[pd.DataFrame]:
    """Yield XLSX rows in bounded batches using openpyxl read-only mode."""
    from openpyxl import load_workbook

    workbook = load_workbook(file_path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        header_row = next(rows, None)
        if header_row is None:
            yield pd.DataFrame()
            return
        headers = ["" if value is None else str(value) for value in header_row]
        batch: list[tuple[Any, ...]] = []
        for row in rows:
            batch.append(tuple("" if value is None else str(value) for value in row))
            if len(batch) >= IMPORT_CHUNK_ROWS:
                yield pd.DataFrame(batch, columns=headers)
                batch = []
        if batch:
            yield pd.DataFrame(batch, columns=headers)
        elif not headers:
            yield pd.DataFrame()
    finally:
        workbook.close()


def _iter_data_frames(file_path: str, filename: str) -> Iterator[pd.DataFrame]:
    """Yield import data frames without materializing CSV/XLSX files in memory."""
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".csv":
        encoding = _detect_encoding(file_path)
        yield from pd.read_csv(
            file_path,
            encoding=encoding,
            dtype=str,
            keep_default_na=False,
            chunksize=IMPORT_CHUNK_ROWS,
        )
        return
    if ext == ".xlsx":
        yield from _iter_xlsx_frames(file_path)
        return
    if ext == ".xls":
        # Legacy XLS has no streaming reader in the supported dependency set.
        # It remains supported, but modern large files should use CSV/XLSX.
        yield _read_df(file_path, filename)
        return
    raise ValueError(f"不支持的文件格式: {ext}")


def _infer_type(series: pd.Series) -> str:
    """Infer a PG column type from a sample of string values."""
    vals = [str(v).strip() for v in series.head(200).tolist() if pd.notna(v) and str(v).strip()]
    if not vals:
        return "TEXT"
    non_empty = vals
    # Boolean
    if all(v.lower() in ("true", "false", "1", "0", "是", "否", "yes", "no", "y", "n") for v in non_empty) and set(non_empty) <= {"0", "1", "是", "否", "true", "false", "yes", "no", "y", "n"}:
        return "BOOLEAN"
    # Integer
    if all(re.fullmatch(r"[+-]?\d+", v) for v in non_empty):
        return "INTEGER" if len(set(non_empty)) < 2_000_000_000 else "BIGINT"
    # Numeric
    if all(re.fullmatch(r"[+-]?(\d+\.\d+|\d+)", v) for v in non_empty):
        return "NUMERIC(20,4)"
    # Date
    date_ok = 0
    for v in non_empty:
        for fmt in DATE_FORMATS:
            try:
                datetime.strptime(v, fmt)
                date_ok += 1
                break
            except ValueError:
                continue
    if date_ok == len(non_empty):
        return "DATE"
    # Timestamp
    ts_ok = 0
    for v in non_empty:
        for fmt in TIMESTAMP_FORMATS:
            try:
                datetime.strptime(v, fmt)
                ts_ok += 1
                break
            except ValueError:
                continue
    if ts_ok == len(non_empty):
        return "TIMESTAMP"
    return "TEXT"


def _snake_case(name: str) -> str:
    """Preserve Chinese/CJK characters, only sanitize dangerous chars."""
    s = name.strip()
    # Replace only truly dangerous chars, keep CJK and alphanumeric
    s = re.sub(r'[\'\";\\`/]', '_', s)
    # Collapse multiple underscores/spaces
    s = re.sub(r'[_\s]+', '_', s)
    s = s.strip('_')
    return s.lower() if s.isascii() else s


def _parse_value(value: Any, col_type: str) -> Any:
    if pd.isna(value) or value == "" or value is None:
        return None
    try:
        if col_type == "TEXT":
            return str(value).strip()
        elif col_type in ("INTEGER", "BIGINT"):
            return int(float(str(value).strip()))
        elif col_type.startswith("NUMERIC"):
            return float(str(value).strip())
        elif col_type == "BOOLEAN":
            return str(value).strip().lower() in ("true", "1", "是", "yes", "y")
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


def _ensure_unique_col_names(headers: list[str]) -> list[str]:
    """Sanitize headers and ensure uniqueness by appending _2, _3, ..."""
    seen: set[str] = set()
    result: list[str] = []
    base_names = [_snake_case(h) for h in headers]
    for i, base in enumerate(base_names):
        name = base or f"col_{i + 1}"
        candidate = name
        n = 2
        while candidate in seen:
            candidate = f"{name}_{n}"
            n += 1
        seen.add(candidate)
        result.append(candidate)
    return result


def _resolve_column_type(
    df: pd.DataFrame,
    cname: str,
    requested: str | None,
) -> str:
    """Return the PG type for one column.

    A caller-pinned type always wins. Otherwise the column is TEXT: inferring a
    narrower type from a sample of values silently breaks identifiers that look
    numeric but are not numbers (an 11-digit phone number inferred as INTEGER
    then fails every insert with "integer out of range"). Passing ``AUTO``
    explicitly restores inference for callers who really want it.
    """
    normalized = (requested or "").strip().upper()
    if normalized and normalized != COLUMN_TYPE_AUTO:
        return ALLOWED_COLUMN_TYPES.get(normalized) or "TEXT"
    if normalized == COLUMN_TYPE_AUTO:
        return _infer_type(df[cname])
    return "TEXT"


def _create_real_table(
    hg_conn,
    table_name: str,
    col_names: list[str],
    df: pd.DataFrame,
    comments: list[str],
    requested_types: list[str | None] | None = None,
) -> dict:
    """Create the real PG table with id BIGSERIAL PK + resolved column types."""
    # BIGSERIAL keeps account databases self-contained. Unlike gen_random_uuid,
    # it does not depend on pgcrypto being installed in every new user database.
    col_defs = ['id BIGSERIAL PRIMARY KEY']
    types: list[str] = []
    requested_types = requested_types or []
    for i, cname in enumerate(col_names):
        requested = requested_types[i] if i < len(requested_types) else None
        ctype = _resolve_column_type(df, cname, requested)
        types.append(ctype)
        col_defs.append(f'"{cname}" {ctype}')
    ddl = f'CREATE TABLE "{table_name}" ({", ".join(col_defs)})'
    with hg_conn.cursor() as cur:
        cur.execute(ddl)
        for i, cname in enumerate(col_names):
            if comments[i]:
                cur.execute(
                    f'COMMENT ON COLUMN "{table_name}"."{cname}" IS %s',
                    (comments[i],),
                )
        cur.execute(f'COMMENT ON TABLE "{table_name}" IS %s', (table_name,))
        hg_conn.commit()
    return dict(zip(col_names, types))


def _load_persisted_schema(table_id: str) -> tuple[list[str], dict[str, str]] | None:
    """Return the stored column names/types for a table, or ``None``.

    ``None`` means the registry still reports this table as uninitialized, so
    the caller must create the real table instead of reusing a schema.
    """
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT initialized, columns FROM {SYSTEM_TABLE_REGISTRY} WHERE id=%s",
                (table_id,),
            )
            row = cur.fetchone()
    finally:
        _put_conn(conn)
    if not row or not row["initialized"]:
        return None
    persisted = row["columns"] or []
    col_names = [c["column_name"] for c in persisted]
    types = {c["column_name"]: c["type"] for c in persisted}
    if not col_names:
        return None
    return col_names, types


def _batch_insert(hg_conn, table_name: str, col_names: list[str], df: pd.DataFrame, types: list[str], batch_size: int = 1000) -> tuple[int, list[dict]]:
    placeholders = ", ".join(["%s"] * len(col_names))
    # 必须对列名逐一双引号，否则 PG 会把 订单ID 这样的混合大小写列名
    # 折叠成小写 订单id，导致查询不到列。_create_real_table 里建表时已加引号。
    quoted_cols = ", ".join('"' + c + '"' for c in col_names)
    insert_sql = f'INSERT INTO "{table_name}" ({quoted_cols}) VALUES ({placeholders})'
    success = 0
    errors: list[dict] = []
    for start in range(0, len(df), batch_size):
        batch = df.iloc[start : start + batch_size]
        values = []
        for _, row in batch.iterrows():
            vals = []
            for i, cname in enumerate(col_names):
                vals.append(_parse_value(row[cname], types[i]))
            values.append(tuple(vals))
        try:
            with hg_conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, insert_sql, values, page_size=batch_size)
            hg_conn.commit()
            success += len(batch)
        except Exception as e:
            hg_conn.rollback()
            errors.append({"row": start, "column": "", "message": f"批次写入失败: {str(e)[:200]}"})
            if len(errors) >= 100:
                break
    return success, errors


def _process_file_headers(df: pd.DataFrame, user_comments: list[str], filename: str) -> tuple[list[str], list[str]]:
    """Return (actual_column_names, comments_by_column).

    If user provided comments, map them by order. Otherwise use header as comment.
    Real column names always come from the file headers.
    """
    raw_headers = list(df.columns)
    col_names = _ensure_unique_col_names(raw_headers)
    # Rename df columns to sanitized names so downstream reads use them
    df.rename(columns=dict(zip(raw_headers, col_names)), inplace=True)
    comments = [c.strip() for c in (user_comments or [])]
    # Pad or truncate comments to match columns
    padded: list[str] = []
    for i in range(len(col_names)):
        if i < len(comments):
            padded.append(comments[i])
        else:
            padded.append(col_names[i])
    return col_names, padded


def _remap_frame_columns(df: pd.DataFrame, col_names: list[str]) -> pd.DataFrame:
    """Map a later import batch to the persisted schema by column position."""
    raw_headers = list(df.columns)
    if len(raw_headers) != len(col_names):
        raise ValueError(f"文件列数不匹配：数据表有 {len(col_names)} 列，文件有 {len(raw_headers)} 列")
    remapped = df.copy()
    remapped.columns = col_names
    return remapped


def run_import_job(job_id: str, table_id: str, file_path: str, filename: str):
    from .store import _get_conn as get_sys_conn, _put_conn as put_sys_conn

    conn = get_sys_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {SYSTEM_TABLE_IMPORT_JOB} SET status='parsing', attempts=attempts+1, started_at=COALESCE(started_at, now()), next_retry_at=NULL WHERE id=%s", (job_id,))
            conn.commit()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM {SYSTEM_TABLE_REGISTRY} WHERE id=%s", (table_id,))
            table = cur.fetchone()
            if not table:
                raise RuntimeError(f"表不存在: {table_id}")
            user_comments = [c.get("comment", "") for c in (table["columns"] or [])]
            # Column types pinned when the table was defined (TEXT by default).
            requested_types = [
                c.get("type") for c in (table["columns"] or [])
            ]
            db_name = table["db_name"]
            table_name = table["table_name"]
    finally:
        put_sys_conn(conn)

    try:
        frames = iter(_iter_data_frames(file_path, filename))
        first_frame = next(frames, None)
        if first_frame is None:
            raise ValueError("上传文件为空或没有表头")
        total = 0
        conn = get_sys_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {SYSTEM_TABLE_IMPORT_JOB} SET status='loading', total_rows=0 WHERE id=%s", (job_id,))
                conn.commit()
        finally:
            put_sys_conn(conn)

        db_name = ensure_user_database(table["owner_username"]) if not db_name else db_name
        # Path scoping: create the physical table inside the schema of the path it
        # was registered under, so the data lands where the listing and the query
        # tools expect it. An unbound row (path_id = '') is a pre-existing table
        # and belongs to the account root, which is the ``public`` schema — the
        # same normalisation used everywhere else, and why no pre-existing table
        # has to move on upgrade.
        table_schema = _schema_for_registry_row(table)
        # Bound before the try so the ``finally`` below can always tell whether a
        # connection was actually acquired. ``ensure_path_schema`` runs first and
        # can raise (a schema/permission/database failure); the earlier shape
        # called it *before* the assignment, so that failure made the ``finally``
        # raise ``UnboundLocalError`` and the real database error was replaced in
        # ``log_excerpt`` — the operator saw "cannot access local variable
        # 'hg_conn'" and every retry reproduced the same masking.
        hg_conn = None
        try:
            # The table's schema is decided once, by whichever job gets here
            # first for an uninitialized table. Concurrent jobs would otherwise
            # each CREATE TABLE and all but one would fail on the duplicate
            # object error, so the check must be re-done *inside* the lock: the
            # value read before the lock is stale as soon as another job wins.
            ensure_path_schema(db_name, table_schema)
            hg_conn = get_target_connection(db_name, table_schema)
            with _init_lock_for(table_id):
                persisted_schema = _load_persisted_schema(table_id)
                if persisted_schema is None:
                    col_names, comments = _process_file_headers(
                        first_frame, user_comments, filename,
                    )
                    types = _create_real_table(
                        hg_conn,
                        table_name,
                        col_names,
                        first_frame,
                        comments,
                        requested_types=requested_types,
                    )
                    # Persist the real schema so later jobs reuse it instead of
                    # creating the table again.
                    conn = get_sys_conn()
                    try:
                        new_columns = []
                        for i, cname in enumerate(col_names):
                            new_columns.append({
                                "comment": comments[i],
                                "column_name": cname,
                                "type": types[cname],
                                "nullable": True,
                                "is_primary_key": False,
                            })
                        with conn.cursor() as cur:
                            cur.execute(
                                f"UPDATE {SYSTEM_TABLE_REGISTRY} SET columns=%s, db_name=%s, initialized=true, updated_at=now() WHERE id=%s",
                                (json.dumps(new_columns), db_name, table_id),
                            )
                            conn.commit()
                    finally:
                        put_sys_conn(conn)
                else:
                    col_names, types = persisted_schema
                    first_frame = _remap_frame_columns(first_frame, col_names)

            success = 0
            insert_errors: list[dict] = []
            for frame in chain((first_frame,), frames):
                frame = _remap_frame_columns(frame, col_names)
                total += len(frame)
                inserted, errors = _batch_insert(
                    hg_conn,
                    table_name,
                    col_names,
                    frame,
                    [types[c] for c in col_names],
                )
                success += inserted
                insert_errors.extend(errors)
                if len(insert_errors) >= 100:
                    break
            status = "completed" if not insert_errors else ("partial" if success > 0 else "failed")
        finally:
            if hg_conn is not None:
                put_target_connection(hg_conn, db_name, table_schema)

        conn = get_sys_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {SYSTEM_TABLE_IMPORT_JOB} SET
                        status=%s, total_rows=%s, success_rows=%s, failed_rows=%s, errors=%s, finished_at=now()
                    WHERE id=%s
                """,
                    (status, total, success, total - success, json.dumps(insert_errors[:100]), job_id),
                )
                cur.execute(f"UPDATE {SYSTEM_TABLE_REGISTRY} SET row_count = row_count + %s, updated_at=now() WHERE id=%s", (success, table_id))
                conn.commit()
        finally:
            put_sys_conn(conn)
        cleanup_staged_file(file_path)
    except Exception as e:
        log_excerpt = traceback.format_exc()
        # Default to "still owed a retry" so an error while recording the failure
        # never causes the staged file to be deleted before it is last needed.
        retry = True
        conn = get_sys_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT attempts FROM {SYSTEM_TABLE_IMPORT_JOB} WHERE id=%s", (job_id,))
                row = cur.fetchone()
                attempts = int(row[0] if row else 1)
                retry = attempts <= 3
                cur.execute(
                    f"""UPDATE {SYSTEM_TABLE_IMPORT_JOB} SET status=%s, errors=%s,
                        log_excerpt=%s, next_retry_at=%s,
                        finished_at=CASE WHEN %s THEN NULL ELSE now() END WHERE id=%s""",
                    ("pending" if retry else "failed",
                     json.dumps([{"row": 0, "column": "", "message": str(e)[:500]}]),
                     log_excerpt[-12000:], datetime.now(timezone.utc) + timedelta(hours=1) if retry else None,
                     retry, job_id),
                )
                conn.commit()
        finally:
            put_sys_conn(conn)
        if not retry:
            # Retries are exhausted and nothing will ever read this file again —
            # the recovery scan only requeues ``pending`` jobs. Without this the
            # staged upload (up to 10GB) stayed on disk for good.
            cleanup_staged_file(file_path)


async def _run_import_job_bounded(
    job_id: str,
    table_id: str,
    file_path: str,
    filename: str,
) -> None:
    """Run one blocking import in a bounded worker thread."""
    async with _get_import_semaphore():
        await asyncio.to_thread(
            run_import_job,
            job_id,
            table_id,
            file_path,
            filename,
        )


async def start_import_task(table_id: str, file_path: str, filename: str) -> str:
    job_id = str(uuid.uuid4())
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {SYSTEM_TABLE_IMPORT_JOB} (id, table_id, filename, status, file_path, attempts)
                VALUES (%s, %s, %s, 'pending', %s, 0)
            """,
                (job_id, table_id, filename, file_path),
            )
            conn.commit()
    finally:
        _put_conn(conn)
    if not _schedule_job(job_id, table_id, file_path, filename):
        # This coroutine runs on the event loop, so a failure here means the loop
        # is shutting down. The row is already committed and the recovery loop
        # (or the next start-up) will pick it up, so the upload is not lost —
        # but it will not run right now, and saying nothing would look like a
        # silent stall.
        logger.warning(
            "data import: job %s committed but could not be started now; "
            "the recovery loop will pick it up",
            job_id,
        )
    return job_id


def _schedule_job(job_id: str, table_id: str, file_path: str, filename: str) -> bool:
    """Start a job's runner task. Returns whether a task was actually created.

    **The in-use mark is rolled back if the task could not be created.** This is
    what makes the caller safe to run off the event loop: startup recovery is
    invoked via ``asyncio.to_thread(recover_import_jobs)``, i.e. in a worker thread
    with no running loop, where ``create_task`` raises ``RuntimeError``. An earlier
    revision added the id to ``_scheduled_jobs`` *before* creating the task and
    relied on the task's ``finally`` to remove it — so the raise left the id marked
    forever. The 60-second recovery loop then hit the ``already scheduled``
    early-return for that id on every pass and skipped it, while still counting it
    as scheduled, so the job sat at ``pending`` for good and a restart re-poisoned
    it. Failing the whole app start is not an option either, so the mark is undone
    here instead and the caller decides what to do.

    Returns ``True`` when a runner task was created (and will discard the mark
    when it finishes), ``False`` when it could not be — including "already
    scheduled", so callers can tell "handled" from "not handled".
    """
    if job_id in _scheduled_jobs:
        return True
    _scheduled_jobs.add(job_id)

    async def runner() -> None:
        try:
            await _run_import_job_bounded(job_id, table_id, file_path, filename)
        finally:
            _scheduled_jobs.discard(job_id)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop in this thread (startup recovery runs in a worker
        # thread via ``asyncio.to_thread``). Undo the mark so a later attempt from
        # the event loop can still schedule this job instead of finding it
        # permanently "in use".
        #
        # Checked before building the coroutine: constructing it and letting
        # ``create_task`` fail leaves an un-awaited coroutine object, which Python
        # warns about and which reads like a bug in the logs.
        _scheduled_jobs.discard(job_id)
        logger.warning(
            "data import: no event loop in this thread; job %s left for the "
            "recovery loop",
            job_id,
        )
        return False

    loop.create_task(runner(), name=f"data-import-{job_id}")
    return True


def recover_import_jobs() -> int:
    """Requeue interrupted jobs and schedule queued jobs after app restart.

    Returns the number of jobs whose runner task was **actually created** — not
    the number of rows that looked eligible. The distinction matters because the
    count is the only signal that this scan did anything: an earlier revision
    incremented it after calling a ``_schedule_job`` that had silently failed
    (``create_task`` with no running loop), so the log reported progress while
    every job stayed ``pending`` and the operator had nothing to go on.

    This is the *only* path that retries a job after it failed: a failed job is
    parked as ``pending`` with a future ``next_retry_at`` and is rescheduled here
    once that time passes. A crash inside this function therefore does not just
    skip a scan — it stops every parked job from ever being retried, which is how
    a couple of failed imports became permanently stuck.
    """
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""UPDATE {SYSTEM_TABLE_IMPORT_JOB}
                SET status='pending', next_retry_at=NULL
                WHERE status IN ('parsing','loading')""")
            cur.execute(f"""SELECT id, table_id, file_path, filename FROM {SYSTEM_TABLE_IMPORT_JOB}
                WHERE status='pending' AND file_path IS NOT NULL
                AND (next_retry_at IS NULL OR next_retry_at <= now())""")
            rows = cur.fetchall()
            conn.commit()
    finally:
        _put_conn(conn)

    scheduled = 0
    for row in rows:
        # Per-row guard: one unusable row (missing file, bad path) must not stop
        # the remaining pending jobs from being rescheduled.
        try:
            if not Path(row["file_path"]).is_file():
                logger.warning(
                    "data import recovery: staged file is gone for job %s (%s)",
                    row["id"],
                    row["file_path"],
                )
                continue
            if _schedule_job(
                str(row["id"]),
                str(row["table_id"]),
                row["file_path"],
                row["filename"],
            ):
                scheduled += 1
        except Exception:  # noqa: BLE001 - keep the scan going
            logger.exception(
                "data import recovery: failed to schedule job %s",
                row.get("id"),
            )
    if rows and not scheduled:
        # Every eligible row was skipped. Say so, otherwise this looks identical
        # to a healthy scan that found nothing to do.
        logger.warning(
            "data import recovery: %d pending job(s) could not be started "
            "(staged file missing, or no event loop in this thread); they will "
            "be retried by the periodic recovery loop",
            len(rows),
        )
    return scheduled



async def import_recovery_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            recover_import_jobs()
        except Exception:
            logger.exception("data import recovery scan failed")
