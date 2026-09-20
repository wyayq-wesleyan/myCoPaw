"""Agent tools for data table queries against HighGo."""

import asyncio
import re
import logging
import shutil
import time
import uuid

from pathlib import Path

from ...runtime.tool_registry import tool_descriptor
from ...utils.io_utils import run_sync_io
from .user_files import resolve_user_file, user_facing_name

logger = logging.getLogger(__name__)

# Import runs as a background task; wait a bounded time so small files report
# their row count immediately and large ones hand back a job id instead of
# blocking the conversation.
_IMPORT_POLL_TIMEOUT = 45.0
_IMPORT_POLL_INTERVAL = 1.5
_IMPORT_TERMINAL_STATES = frozenset({"completed", "partial", "failed"})

_DANGEROUS_SQL_RE = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


def _sanitize_sql(sql: str) -> str:
    s = _BLOCK_COMMENT_RE.sub("", sql)
    s = _LINE_COMMENT_RE.sub("", s)
    return s.strip()


def _is_read_only(sql: str) -> bool:
    """判断一条 SQL 是否为安全的只读查询。

    仅允许单一的 SELECT / WITH 语句。拒绝：
    - 以 INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/CREATE/GRANT/REVOKE 开头
    - 包含分号的多语句（堆叠查询），例如
      ``SELECT * FROM t; DROP TABLE t`` —— psycopg2 默认会执行整个字符串，
      若不拦截会造成真实破坏。
    - 以非 SELECT/WITH 开头或为空
    """
    sanitized = _sanitize_sql(sql)
    if not sanitized:
        return False
    if _DANGEROUS_SQL_RE.match(sanitized):
        return False
    # 拒绝堆叠/多条语句：分号后仍有多余内容即视为危险，防止 DROP 等混入。
    stripped = sanitized.rstrip(";").strip()
    if ";" in stripped:
        return False
    first_word = stripped.split()[0].upper() if stripped.split() else ""
    return first_word in ("SELECT", "WITH")


def _ensure_limit(sql: str, max_rows: int = 100) -> str:
    """给查询追加 LIMIT 上限，防止单次查询拉取过多行。

    注意：只应在"真正的查询语句末尾"追加 LIMIT。若用户已显式书写 LIMIT
    或 OFFSET（无论大小写、是否出现在字符串里都难以区分），为稳妥起见仅
    在语句末尾没有 LIMIT/OFFSET 关键字时追加。这里使用词边界匹配，避免
    把字符串里的 "LIMIT" 误判为查询自身的 LIMIT。
    """
    sanitized = _sanitize_sql(sql).rstrip(";")
    # 用词边界匹配真正的 LIMIT/OFFSET 关键字，而不是简单子串匹配，
    # 避免 SELECT 'LIMIT' AS x 这类字符串内容误判。
    if re.search(r"\b(LIMIT|OFFSET)\b", sanitized, re.IGNORECASE):
        return sql
    return f"{sanitized} LIMIT {max_rows}"


def _resolve_username() -> str:
    from ...app.agent_context import get_current_user_id

    username = get_current_user_id()
    if username and username != "default":
        return username
    return "admin"


@tool_descriptor(
    name="list_data_tables",
    description="列出当前用户创建的所有数据表名称、描述、列信息和行数。在需要了解有哪些可用数据表时使用。",
    parameters={"type": "object", "properties": {}, "required": []},
    tool_type="internal",
    policy_name="ListDataTables",
    default_policy="allow",
    policy_reason="Read-only listing of the current user's data tables",
)
def list_data_tables() -> str:
    username = _resolve_username()
    from qwenpaw.data_tables.store import SYSTEM_TABLE_REGISTRY, _get_conn, _put_conn

    conn = _get_conn()
    try:
        import psycopg2.extras

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT table_name, description, columns, row_count, initialized
                FROM {SYSTEM_TABLE_REGISTRY}
                WHERE owner_username = %s
                ORDER BY created_at DESC
            """,
                (username,),
            )
            rows = cur.fetchall()
            if not rows:
                return "当前用户没有已创建的数据表。请先通过「数据分析」菜单创建数据表并导入数据。"
            parts = []
            for r in rows:
                cols_desc = ", ".join(
                    (c.get("comment", "") or c.get("column_name", "")) for c in (r["columns"] or [])
                )
                status = "已导入" if r["initialized"] else "待导入"
                parts.append(
                    f"- **{r['table_name']}**（{status}）：{r['description'] or '无描述'}，"
                    f"{len(r['columns'] or [])} 列，{r['row_count'] or 0} 行。列含义：{cols_desc}"
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
            },
        },
        "required": ["table_name"],
    },
    tool_type="internal",
    policy_name="DescribeDataTable",
    default_policy="allow",
    policy_reason="Read-only inspection of the current user's data table",
)
def describe_data_table(table_name: str) -> str:
    username = _resolve_username()
    from qwenpaw.data_tables.store import SYSTEM_TABLE_REGISTRY, _get_conn, _put_conn
    from qwenpaw.data_tables.connection import get_target_connection, put_target_connection

    conn = _get_conn()
    try:
        import psycopg2.extras

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT * FROM {SYSTEM_TABLE_REGISTRY}
                WHERE owner_username = %s AND table_name = %s
            """,
                (username, table_name),
            )
            table = cur.fetchone()
            if not table:
                return f"表 {table_name} 不存在。请先用 list_data_tables 查看可用表。"
    finally:
        _put_conn(conn)

    columns = table["columns"] or []
    # If not yet imported, only comments/descriptions are known
    if not table["initialized"]:
        col_details = []
        for c in columns:
            comment = c.get("comment", "") or ""
            col_details.append(f"- 列含义：{comment}")
        return (
            f"## 表 {table_name}\n"
            f"{table['description'] or '无描述'}\n\n"
            f"### 列说明（等待导入数据后自动推断列名和类型）\n"
            + ("\n".join(col_details) if col_details else "（暂无列说明）")
        )

    col_defs = []
    pk_cols = [c["column_name"] for c in columns if c.get("is_primary_key")]
    for col in columns:
        cname = col.get("column_name")
        if not cname:
            continue
        parts = [f'"{cname}" {col["type"]}']
        if not col.get("nullable", True):
            parts.append("NOT NULL")
        if col.get("comment"):
            parts.append(f"/* {col['comment']} */")
        col_defs.append(" ".join(parts))
    if pk_cols:
        col_defs.append(f"PRIMARY KEY ({', '.join(pk_cols)})")
    ddl = f"CREATE TABLE {table_name} (\n  " + ",\n  ".join(col_defs) + "\n);"

    col_details = []
    for c in columns:
        cname = c.get("column_name", "")
        if not cname:
            continue
        col_details.append(f"- {cname}（{c['type']}）：{c.get('comment', '无说明')}")

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
            },
        },
        "required": ["sql"],
    },
    tool_type="internal",
    policy_name="QueryDataTable",
    default_policy="allow",
    policy_reason="Read-only SQL query scoped to the current user's database",
)
def query_data_table(sql: str) -> str:
    if not _is_read_only(sql):
        return "安全限制：仅允许 SELECT 查询。不允许 INSERT/UPDATE/DELETE/DROP 等写入操作。"
    username = _resolve_username()
    sql = _ensure_limit(sql, 100)

    from qwenpaw.data_tables.connection import (
        get_target_connection,
        put_target_connection,
        ensure_user_database,
    )

    db_name = ensure_user_database(username)
    hg_conn = get_target_connection(db_name)
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
        put_target_connection(hg_conn, db_name)


def _create_table_sync(username: str, description: str, display_name: str) -> dict:
    from qwenpaw.data_tables.table_api import create_table_record

    return create_table_record(
        username,
        display_name=display_name,
        description=description,
    )


def _find_table_sync(username: str, table_ref: str) -> dict | None:
    from qwenpaw.data_tables.table_api import find_owned_table

    return find_owned_table(username, table_ref)


def _job_status_sync(job_id: str) -> dict | None:
    import psycopg2.extras

    from qwenpaw.data_tables.store import (
        SYSTEM_TABLE_IMPORT_JOB,
        _get_conn,
        _put_conn,
    )

    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT status, total_rows, success_rows, failed_rows, errors"
                f" FROM {SYSTEM_TABLE_IMPORT_JOB} WHERE id::text = %s",
                (job_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        _put_conn(conn)


def _stage_import_file(source: Path, username: str) -> Path:
    """Copy an upload into the calling account's import staging area.

    The importer deletes its source file once the import succeeds, so the chat
    attachment has to be copied rather than handed over directly. The copy goes
    under the account's own subtree, not the shared staging root.
    """
    from qwenpaw.data_tables.store import user_upload_dir

    target_dir = Path(user_upload_dir(username)) / uuid.uuid4().hex
    target_dir.mkdir(parents=True, exist_ok=True)
    staged = target_dir / source.name
    shutil.copy2(source, staged)
    return staged


def _describe_import_state(status: dict) -> str:
    success = status.get("success_rows") or 0
    failed = status.get("failed_rows") or 0
    total = status.get("total_rows") or 0
    if failed:
        detail = _first_import_error(status.get("errors"))
        lines = [f"导入完成但部分行失败：成功 {success} 行，失败 {failed} 行。"]
        if detail:
            lines.append(f"失败样例：{detail}")
        return "\n".join(lines)
    rows = f"成功 {success} 行"
    if total:
        rows += f"（共 {total} 行）"
    return f"导入完成：{rows}。"


def _first_import_error(errors: object) -> str:
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return str(first.get("error") or first)[:300]
        return str(first)[:300]
    return ""


async def _await_import(job_id: str, table_name: str, filename: str) -> str:
    deadline = time.monotonic() + _IMPORT_POLL_TIMEOUT
    status: dict = {}
    while True:
        status = await run_sync_io(_job_status_sync, job_id) or {}
        if status.get("status") in _IMPORT_TERMINAL_STATES:
            break
        if time.monotonic() >= deadline:
            return (
                f"已开始导入：{filename} → {table_name}（任务 {job_id}）。\n"
                f"当前状态：{status.get('status') or 'pending'}。"
                "文件较大时导入需要时间，稍后用 describe_data_table 查看行数即可。"
            )
        await asyncio.sleep(_IMPORT_POLL_INTERVAL)

    if status.get("status") == "failed":
        detail = _first_import_error(status.get("errors"))
        return (
            f"导入失败：{filename} → {table_name}。"
            + (f"原因：{detail}" if detail else "请检查文件内容是否符合表格格式。")
        )
    return f"{filename} → {table_name}，" + _describe_import_state(status)


@tool_descriptor(
    name="create_data_table",
    description=(
        "在当前用户的数据分析中新建一张数据表。仅在用户上传了表格文件且明确要求建表、"
        "入库或以后能直接查询时使用。表名由描述自动生成，列名和类型会在导入文件时按文件"
        "表头自动推断，所以这里不需要提供列定义。创建后用 import_data_table 导入数据。"
    ),
    async_execution=True,
    tool_type="file",
    policy_name="CreateDataTable",
    default_policy="ask",
    policy_reason="Creates a persistent data table in the current account",
    ui_description="在当前用户的数据分析中新建数据表",
    ui_icon="🗂️",
)
async def create_data_table(description: str, display_name: str = "") -> str:
    """Create a data table definition for the current account.

    Args:
        description: 这张表的中文描述，用于生成表名并展示在数据分析页面，
            例如"2024 年各区域销售明细"。写清楚数据来源和含义，便于以后查询。
        display_name: 可选的展示名称，留空则与描述一致。
    """
    description = (description or "").strip()
    display_name = (display_name or "").strip()
    if not description:
        return "请先说明这张表是做什么用的（表描述），表名会根据描述自动生成。"

    username = _resolve_username()
    try:
        record = await run_sync_io(_create_table_sync, username, description, display_name)
    except Exception as exc:  # Keep failures readable to the model.
        detail = getattr(exc, "detail", None) or str(exc)
        return f"新建数据表失败：{detail}"

    return (
        f"已创建数据表 {record['table_name']}：{description}\n"
        f"接下来调用 import_data_table，table_name 传 \"{record['table_name']}\"，"
        f"file 传用户的表格文件名，即可把数据导入这张表。"
    )


@tool_descriptor(
    name="import_data_table",
    description=(
        "把用户上传的表格文件（CSV/XLSX/XLS）导入当前用户已有的数据表中。"
        "文件必须属于当前用户的工作区，例如用户在聊天框里上传的附件。"
        "需要先有数据表（用 create_data_table 新建或 list_data_tables 查看已有的表）。"
    ),
    async_execution=True,
    tool_type="file",
    target_param="file",
    policy_name="ImportDataTable",
    default_policy="ask",
    policy_reason="Imports a user file into the current account's data table",
    ui_description="把用户上传的表格文件导入数据表",
    ui_icon="📥",
)
async def import_data_table(table_name: str, file: str) -> str:
    """Import one tabular file into a table owned by the current account.

    Args:
        table_name: 目标数据表名称（list_data_tables 或 create_data_table 返回的表名）。
        file: 要导入的文件名或路径，例如"销售数据.xlsx"。聊天附件直接传文件名即可。
    """
    username = _resolve_username()
    table = await run_sync_io(_find_table_sync, username, table_name)
    if not table:
        return (
            f"没有找到属于你的数据表 {table_name}。"
            "请先用 list_data_tables 查看已有表，或先用 create_data_table 新建一张。"
        )

    # Kept lazy: the tools package must import without the deploy-only DB stack.
    from qwenpaw.data_tables.store import (
        ALLOWED_EXTENSIONS,
        MAX_IMPORT_FILE_BYTES,
        MAX_XLS_FILE_BYTES,
    )

    path, error = resolve_user_file(file, frozenset(ALLOWED_EXTENSIONS))
    if path is None:
        return error

    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"读取文件失败：{exc}"
    suffix = path.suffix.lower()
    if suffix == ".xls" and size > MAX_XLS_FILE_BYTES:
        return "传统 XLS 文件不能超过 512MB，请先转换为 CSV 或 XLSX 再导入。"
    if size > MAX_IMPORT_FILE_BYTES:
        return "单个文件不能超过 10GB。"

    try:
        staged = await run_sync_io(_stage_import_file, path, username)
    except Exception as exc:
        return f"准备导入文件失败：{exc}"

    display_name = user_facing_name(path)
    try:
        from qwenpaw.data_tables.importer import start_import_task

        job_id = await start_import_task(str(table["id"]), str(staged), display_name)
    except Exception as exc:
        return f"启动导入任务失败：{exc}"

    return await _await_import(
        job_id,
        table.get("table_name") or table_name,
        display_name,
    )
