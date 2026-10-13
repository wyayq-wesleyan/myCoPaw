"""Agent tools for data table queries against HighGo."""

import asyncio
import logging
import re

import psycopg2
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


_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
# Identifiers: double-quoted (PostgreSQL escapes a quote by doubling it) or bare
# word tokens. Used by the schema-scope guard below.
_QUOTED_IDENT_RE = re.compile(r'"((?:[^"]|"")*)"')
_BARE_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
# Functions that read the server filesystem. Not needed for any legitimate query,
# and reachable through a plain SELECT.
_BLOCKED_FUNCS_RE = re.compile(
    r"\b(pg_read_file|pg_read_binary_file|pg_ls_dir|pg_stat_file|lo_import|lo_export)\b",
    re.IGNORECASE,
)


class SchemaScopeError(ValueError):
    """A query reached outside the schemas this agent was granted."""


def _enforce_schema_scope(sql: str, allowed_schemas: list[str], known_schemas: list[str]) -> None:
    """Refuse a query that names a path schema the agent was not granted.

    **Why this exists.** ``search_path`` only decides how *unqualified* names
    resolve — it is a convenience, not a boundary. Any query can name another
    schema directly::

        SELECT * FROM p_<another-case>."tb_secret"

    which ``search_path`` happily allows because the name is already qualified.
    Since ``query_data_table`` executes model-written SQL, that was a real hole:
    an agent granted one case could read another case's tables (verified — it
    returned the other case's rows), and could even enumerate the schemas to find
    them via ``pg_namespace``.

    Rejecting any mention of a schema the agent does not hold closes it. Names are
    UUID-derived, so a legitimate query has no reason to reference a foreign one;
    string literals are stripped first so data values that merely *look* like a
    schema name (and quoted references to allowed schemas) are unaffected.
    """
    if _BLOCKED_FUNCS_RE.search(sql):
        raise SchemaScopeError("查询包含不允许的函数（禁止读取服务器文件）。")

    scrubbed = _STRING_LITERAL_RE.sub("''", _sanitize_sql(sql))
    mentioned: set[str] = set(_QUOTED_IDENT_RE.findall(scrubbed))
    mentioned.update(_BARE_WORD_RE.findall(scrubbed))

    allowed = set(allowed_schemas)
    forbidden = sorted(
        name for name in known_schemas
        # ``public`` is the root path's schema: referencing it is only legitimate
        # when the agent actually holds the root.
        if name not in allowed and name in mentioned
    )
    if forbidden:
        raise SchemaScopeError(
            f"查询引用了未授权的路径 schema（{', '.join(forbidden)}）。"
            "只能查询当前智能体被挂载的路径下的数据表。",
        )


def _known_path_schemas(db_name: str) -> list[str]:
    """Every path schema that exists in one account database.

    Needed by the scope guard: it must know which identifiers *are* schemas, so a
    table alias that happens to look like a name is not mistaken for one.
    """
    from qwenpaw.data_tables.connection import _system_db_config

    try:
        conn = psycopg2.connect(
            host=_system_db_config()["host"],
            port=_system_db_config()["port"],
            dbname=db_name,
            user=_system_db_config()["username"],
            password=_system_db_config()["password"],
            connect_timeout=10,
        )
    except Exception:  # noqa: BLE001 - no database means no schemas to guard
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT nspname FROM pg_namespace "
                "WHERE nspname LIKE %s OR nspname = %s",
                ("p\\_%", "public"),
            )
            return [row[0] for row in cur.fetchall()]
    except Exception:  # noqa: BLE001
        logger.warning(
            "data tables: could not list path schemas for %s", db_name, exc_info=True,
        )
        return []
    finally:
        conn.close()


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


def _table_visible_to_agent(username: str, path_id: str | None) -> bool:
    """Whether one registry row is reachable by the calling agent.

    Shared by the listing and the detail tool so they cannot disagree (a table
    the list shows must be describable, and one it hides must not). An empty
    ``path_id`` is a pre-existing, unbound table — it belongs to the account root
    and is visible only when the root is granted.
    """
    from .path_scope import root_path_id, visible_data_path_ids

    allowed = visible_data_path_ids(username)
    if allowed is None:
        return False
    allowed = set(allowed)
    root = root_path_id(username)
    if path_id:
        return path_id in allowed
    return bool(root) and root in allowed


def _resolve_username() -> str:
    """Resolve the calling account via the shared account rule.

    The shared resolver refuses when authentication is on but the identity
    cannot be determined, rather than assuming the caller is ``admin``.
    """
    from .user_files import resolve_account_username

    return resolve_account_username()


@tool_descriptor(
    name="list_paths",
    description=(
        "列出当前智能体可以访问的路径（案件/数据空间分区），含路径名称与默认区标记。"
        "在需要把数据表或知识文档归入某个案件之前调用，以确认案件是否存在、名称怎么写。"
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    tool_type="internal",
    policy_name="ListPaths",
    default_policy="allow",
    policy_reason="Read-only listing of the paths this agent may reach",
)
def list_paths() -> str:
    """List the paths (cases) this agent is allowed to read and write.

    Exists so the model can discover case names before naming one: without it the
    only way to file material into a case would be for the user to type a raw UUID
    into the chat.
    """
    username = _resolve_username()
    from .path_scope import granted_path_nodes

    nodes = granted_path_nodes(username)
    if nodes is None:
        return (
            "当前智能体没有可访问的路径。请先在「数据分析」中为该智能体挂载"
            "「默认」或某个案件，之后才能读写其中的数据。"
        )
    if not nodes:
        return (
            "当前智能体挂载的路径已被删除，暂时没有可访问的路径。"
            "请在「数据分析」中重新挂载。"
        )

    lines = ["当前智能体可访问的路径："]
    for node in nodes:
        if node["is_root"]:
            lines.append(f"- {node['name']}（默认区，未指定案件时数据放在这里）")
        else:
            lines.append(f"- {node['name']}")
    lines.append(
        "\n把数据表或知识文档归入某个案件时，把上面的名称原样传给对应工具的 "
        "path 参数即可。",
    )
    return "\n".join(lines)


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

    # Path scoping: list only what this agent may actually query. Reading the
    # registry is cheaper than probing schemas, but it must apply the same rule —
    # otherwise the list advertises another case's tables and the model wastes a
    # call discovering it cannot read them.
    from .path_scope import root_path_id, visible_data_path_ids

    allowed_paths = visible_data_path_ids(username)
    if allowed_paths is None:
        return (
            "当前智能体没有可访问的数据分析路径，请先在数据分析中为该智能体"
            "挂载对应的路径。"
        )
    root_id = root_path_id(username)
    # An unbound table (path_id = '') is a pre-existing one and therefore belongs
    # to the root path. It is only visible when the root itself is granted — the
    # same normalisation the query tool relies on, and adding "" unconditionally
    # would expose public tables to an agent granted only a case.
    # Unbound rows (path_id = '') are pre-existing tables and live in the root
    # path, so they are listed only when the root itself is granted — the same
    # rule ``_table_visible_to_agent`` applies for the detail view.
    allowed_paths = set(allowed_paths)
    if root_id and root_id in allowed_paths:
        allowed_paths.add("")

    conn = _get_conn()
    try:
        import psycopg2.extras

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT table_name, description, columns, row_count, initialized,
                       path_id
                FROM {SYSTEM_TABLE_REGISTRY}
                WHERE owner_username = %s AND (path_id = ANY(%s))
                ORDER BY created_at DESC
            """,
                (username, sorted(allowed_paths)),
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
            # Path scoping, applied before any detail is returned: a table in
            # another case must look "not found" rather than leak its DDL, columns
            # and sample rows. Reusing the same predicate as the query tool keeps
            # the two from disagreeing about what is reachable.
            if not _table_visible_to_agent(username, table.get("path_id")):
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

    from .path_scope import data_search_path

    search_path = data_search_path(username)
    if search_path is None:
        return "当前智能体没有可访问的数据分析路径。"
    hg_conn = get_target_connection(table["db_name"], search_path)
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
        put_target_connection(hg_conn, table["db_name"], search_path)

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

    # Path scoping is enforced by the *connection*, not by inspecting the SQL.
    # The connection's search_path lists only the paths this agent was granted, so
    # a table belonging to another case does not resolve at all — it fails with
    # "relation does not exist" rather than being filtered after the fact. That is
    # what makes an arbitrary read-only query safe here.
    from .path_scope import data_search_path

    search_path = data_search_path(username)
    if search_path is None:
        return (
            "当前智能体没有可访问的数据分析路径，请先在数据分析中为该智能体"
            "挂载对应的路径。"
        )

    db_name = ensure_user_database(username)

    # ``search_path`` alone is not a boundary: it only fixes how unqualified names
    # resolve, so a query naming another case's schema directly would still run.
    # Refuse those before executing (see ``_enforce_schema_scope``).
    try:
        _enforce_schema_scope(sql, search_path, _known_path_schemas(db_name))
    except SchemaScopeError as exc:
        return f"查询被拒绝：{exc}"

    hg_conn = get_target_connection(db_name, search_path)
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
        put_target_connection(hg_conn, db_name, search_path)


def _create_table_sync(
    username: str,
    description: str,
    display_name: str,
    path_id: str = "",
) -> dict:
    from qwenpaw.data_tables.table_api import create_table_record

    # ``path_id`` is what files the table into a case. Omitting it is what made
    # every agent-created table land in the account's public area, where an agent
    # granted only a case could not even list the table it had just created.
    return create_table_record(
        username,
        display_name=display_name,
        description=description,
        path_id=path_id,
    )


def _find_table_sync(username: str, table_ref: str) -> dict | None:
    from qwenpaw.data_tables.table_api import find_owned_table

    return find_owned_table(username, table_ref)


def _move_table_sync(table_id: str, path_id: str) -> dict:
    """Move one table's storage and membership to ``path_id``.

    Order matters and mirrors the console endpoint: the physical move runs first,
    so a rejected move leaves nothing half-done, and the binding is written only
    once the data really sits in the target schema.

    ``path_id`` of ``""`` means the public area — ``move_table_for_path`` treats
    that as the root schema, and ``bind_resource`` is skipped because "unbound"
    already means root.
    """
    from qwenpaw.data_tables.store import move_table_for_path

    result = move_table_for_path(table_id, path_id or None)

    from ...platform_store import paths as path_store

    if path_id:
        path_store.bind_resource(path_id, path_store.KIND_DATA_TABLE, table_id)
    else:
        path_store.unbind_resource(path_store.KIND_DATA_TABLE, table_id)
    return result


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
        "在当前用户的数据空间中新建一张数据表。仅在用户上传了表格文件且明确要求建表、"
        "入库或以后能直接查询时使用。表名由描述自动生成，列名和类型会在导入文件时按文件"
        "表头自动推断，所以这里不需要提供列定义。创建后用 import_data_table 导入数据。"
        "如果用户要求放到某个案件下，用 path 参数指定案件名称（可先调用 list_paths 确认）。"
    ),
    async_execution=True,
    tool_type="internal",
    policy_name="CreateDataTable",
    default_policy="ask",
    policy_reason="Creates a persistent data table in the current account",
    ui_description="在当前用户的数据分析中新建数据表",
    ui_icon="🗂️",
)
async def create_data_table(
    description: str,
    display_name: str = "",
    path: str = "",
) -> str:
    """Create a data table definition for the current account.

    Args:
        description: 这张表的中文描述，用于生成表名并展示在数据分析页面，
            例如"2024 年各区域销售明细"。写清楚数据来源和含义，便于以后查询。
        display_name: 可选的展示名称，留空则与描述一致。
        path: 可选，把这张表归入哪个案件；留空表示默认区。可以是案件名称
            （先用 list_paths 查看可用案件），也可以是路径 id。用户没说放哪里
            时留空即可，不要自己猜一个案件。
    """
    description = (description or "").strip()
    display_name = (display_name or "").strip()
    if not description:
        return "请先说明这张表是做什么用的（表描述），表名会根据描述自动生成。"

    username = _resolve_username()

    # Resolve the case *before* creating anything: a bad or ambiguous name must not
    # leave a half-created table behind in the wrong place.
    from .path_scope import PathAmbiguous, PathNotWritable, resolve_writable_path_id

    try:
        path_id = await run_sync_io(
            resolve_writable_path_id, username, path,
        )
    except PathAmbiguous as exc:
        options = "、".join(
            f"{c['name']}（{c['id']}）" for c in exc.candidates
        )
        return f"有多个路径匹配「{path}」：{options}。请指明要用哪一个。"
    except PathNotWritable as exc:
        return str(exc)

    try:
        record = await run_sync_io(
            _create_table_sync, username, description, display_name, path_id,
        )
    except Exception as exc:  # Keep failures readable to the model.
        detail = getattr(exc, "detail", None) or str(exc)
        return f"新建数据表失败：{detail}"

    where = await run_sync_io(_describe_path_label, username, path_id) if path_id else "默认区"
    return (
        f"已创建数据表 {record['table_name']}：{description}\n"
        f"存放位置：{where}\n"
        f"接下来调用 import_data_table，table_name 传 \"{record['table_name']}\"，"
        f"file 传用户的表格文件名，即可把数据导入这张表。"
    )


def _describe_path_label(username: str, path_id: str) -> str:
    """A human-readable label for a path id, for tool output.

    Falls back to the raw id rather than raising: the table is already created by
    the time this runs, so a naming lookup failure must not turn success into an
    error message.
    """
    try:
        from ...platform_store import paths as path_store

        node = path_store.path_by_id(path_id)
        if node and node.get("name"):
            return str(node["name"])
    except Exception:  # noqa: BLE001
        pass
    return path_id


@tool_descriptor(
    name="move_data_table_to_path",
    description=(
        "把已经存在的数据表归入某个案件（或在需要时移回默认区）。"
        "当用户说「把某张表放到案件X下」「这张表归到X案件」时使用。"
        "用 path 指定目标案件名称（可先调用 list_paths 确认）；path 留空表示移回默认区。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "table_name": {
                "type": "string",
                "description": "要移动的数据表名称（list_data_tables 返回的表名）。",
            },
            "path": {
                "type": "string",
                "description": "目标案件名称或路径 id；留空表示移回默认区。",
            },
        },
        "required": ["table_name"],
    },
    async_execution=True,
    tool_type="internal",
    policy_name="MoveDataTableToPath",
    default_policy="ask",
    policy_reason=(
        "Changes which case a data table belongs to; for a table inside this "
        "account only"
    ),
    ui_description="把数据表归入案件",
    ui_icon="📁",
)
async def move_data_table_to_path(table_name: str, path: str = "") -> str:
    """File an existing data table into a case (or move it back to the default area).

    Args:
        table_name: 要移动的数据表名称，用 list_data_tables 里看到的表名。
        path: 目标案件名称或路径 id；留空表示移回默认区。
    """
    table_name = (table_name or "").strip()
    path = (path or "").strip()
    if not table_name:
        return "请说明要移动哪张数据表（table_name）。"

    username = _resolve_username()

    from .path_scope import PathAmbiguous, PathNotWritable, resolve_writable_path_id

    try:
        path_id = await run_sync_io(resolve_writable_path_id, username, path)
    except PathAmbiguous as exc:
        options = "、".join(f"{c['name']}（{c['id']}）" for c in exc.candidates)
        return f"有多个路径匹配「{path}」：{options}。请指明要用哪一个。"
    except PathNotWritable as exc:
        return str(exc)

    table = await run_sync_io(_find_table_sync, username, table_name)
    if table is None:
        return (
            f"没有找到名为「{table_name}」的数据表。"
            f"可以先用 list_data_tables 查看当前可用的表。"
        )

    # Moving *out of* a case the agent cannot see would be a write to a case it has
    # no grant for. Refuse rather than silently relocate someone else's material.
    current_path_id = table.get("path_id") or ""
    from .path_scope import root_path_id, visible_data_path_ids

    allowed = visible_data_path_ids(username)
    if allowed is None:
        return (
            "当前智能体没有可访问的数据分析路径，请先在数据分析中为该智能体"
            "挂载对应的路径。"
        )
    allowed = set(allowed)
    root = root_path_id(username)
    if root and root in allowed:
        allowed.add("")
    if current_path_id not in allowed:
        return (
            f"表「{table['table_name']}」当前不在该智能体可访问的路径内，"
            f"无法移动。请先在数据分析中把这张表所在的路径挂载给该智能体。"
        )

    try:
        result = await run_sync_io(
            _move_table_sync, str(table["id"]), path_id,
        )
    except Exception as exc:  # noqa: BLE001 - readable failure for the model
        detail = getattr(exc, "detail", None) or str(exc)
        return f"移动数据表失败：{detail}"

    label = await run_sync_io(_describe_path_label, username, path_id) if path_id else "默认区"
    note = ""
    if str(result.get("moved")) == "false":
        # The table has no physical table yet (never imported). The binding still
        # applies and the importer will create it in the right schema.
        note = "（该表尚未导入数据，导入时会直接建在目标路径下）"
    return (
        f"已把数据表 {table['table_name']} 归入：{label}{note}"
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
