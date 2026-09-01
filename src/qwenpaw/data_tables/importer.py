"""Async data importer — parse files and lazy-create tables in HighGo."""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Any

import pandas as pd
import psycopg2
import psycopg2.extras

from .connection import (
    ensure_user_database,
    get_target_connection,
    put_target_connection,
    sanitize_column_name,
)
from .store import _get_conn, _put_conn

logger = logging.getLogger(__name__)

DATE_FORMATS = ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%d/%m/%Y", "%m/%d/%Y", "%Y年%m月%d日"]
TIMESTAMP_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y%m%d %H:%M:%S",
    "%Y年%m月%d日 %H:%M:%S",
]


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
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".csv":
        enc = _detect_encoding(file_path)
        return pd.read_csv(file_path, encoding=enc, dtype=str, keep_default_na=False)
    elif ext in (".xls", ".xlsx"):
        return pd.read_excel(file_path, dtype=str, keep_default_na=False)
    else:
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


def _create_real_table(hg_conn, table_name: str, col_names: list[str], df: pd.DataFrame, comments: list[str]) -> dict:
    """Create the real HighGo table with id UUID PK + inferred column types."""
    col_defs = ['id UUID PRIMARY KEY DEFAULT gen_random_uuid()']
    types: list[str] = []
    for i, cname in enumerate(col_names):
        ctype = _infer_type(df[cname])
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


async def run_import_job(job_id: str, table_id: str, file_path: str, filename: str):
    from .store import _get_conn as get_sys_conn, _put_conn as put_sys_conn

    conn = get_sys_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE data_import_job SET status='parsing' WHERE id=%s", (job_id,))
            conn.commit()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM data_table_registry WHERE id=%s", (table_id,))
            table = cur.fetchone()
            if not table:
                raise RuntimeError(f"表不存在: {table_id}")
            user_comments = [c.get("comment", "") for c in (table["columns"] or [])]
            db_name = table["db_name"]
            table_name = table["table_name"]
            already_initialized = table["initialized"]
    finally:
        put_sys_conn(conn)

    try:
        df = _read_df(file_path, filename)
        total = len(df)
        conn = get_sys_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE data_import_job SET status='loading', total_rows=%s WHERE id=%s", (total, job_id))
                conn.commit()
        finally:
            put_sys_conn(conn)

        db_name = ensure_user_database(table["owner_username"]) if not db_name else db_name
        hg_conn = get_target_connection(db_name)
        try:
            if not already_initialized:
                col_names, comments = _process_file_headers(df, user_comments, filename)
                types = _create_real_table(hg_conn, table_name, col_names, df, comments)
                # persist real schema to registry
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
                            "UPDATE data_table_registry SET columns=%s, db_name=%s, initialized=true, updated_at=now() WHERE id=%s",
                            (json.dumps(new_columns), db_name, table_id),
                        )
                        conn.commit()
                finally:
                    put_sys_conn(conn)
            else:
                # Load persisted schema
                conn = get_sys_conn()
                try:
                    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                        cur.execute("SELECT columns FROM data_table_registry WHERE id=%s", (table_id,))
                        persisted = cur.fetchone()
                        persisted_cols = persisted["columns"] or []
                    col_names = [c["column_name"] for c in persisted_cols]
                    types = {c["column_name"]: c["type"] for c in persisted_cols}
                finally:
                    put_sys_conn(conn)
                # Re-map DataFrame columns to match persisted schema
                raw_headers = list(df.columns)
                if len(raw_headers) >= len(col_names):
                    rename_map = {}
                    for i, cname in enumerate(col_names):
                        if i < len(raw_headers) and raw_headers[i] != cname:
                            rename_map[raw_headers[i]] = cname
                    if rename_map:
                        df.rename(columns=rename_map, inplace=True)
            success, insert_errors = _batch_insert(hg_conn, table_name, col_names, df, [types[c] for c in col_names])
            status = "completed" if not insert_errors else ("partial" if success > 0 else "failed")
        finally:
            put_target_connection(hg_conn, db_name)

        conn = get_sys_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE data_import_job SET
                        status=%s, success_rows=%s, failed_rows=%s, errors=%s, finished_at=now()
                    WHERE id=%s
                """,
                    (status, success, total - success, json.dumps(insert_errors[:100]), job_id),
                )
                cur.execute("UPDATE data_table_registry SET row_count = row_count + %s, updated_at=now() WHERE id=%s", (success, table_id))
                conn.commit()
        finally:
            put_sys_conn(conn)
        try:
            os.remove(file_path)
        except OSError:
            pass
    except Exception as e:
        conn = get_sys_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE data_import_job SET status='failed', errors=%s, finished_at=now() WHERE id=%s",
                    (json.dumps([{"row": 0, "column": "", "message": str(e)[:500]}]), job_id),
                )
                conn.commit()
        finally:
            put_sys_conn(conn)


async def start_import_task(table_id: str, file_path: str, filename: str) -> str:
    job_id = str(uuid.uuid4())
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO data_import_job (id, table_id, filename, status)
                VALUES (%s, %s, %s, 'pending')
            """,
                (job_id, table_id, filename),
            )
            conn.commit()
    finally:
        _put_conn(conn)
    asyncio.create_task(run_import_job(job_id, table_id, file_path, filename))
    return job_id
