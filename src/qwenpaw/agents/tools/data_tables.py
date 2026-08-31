"""Agent tools for data table queries against HighGo."""

import re
import logging

from ...runtime.tool_registry import tool_descriptor

logger = logging.getLogger(__name__)

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
    sanitized = _sanitize_sql(sql)
    if _DANGEROUS_SQL_RE.match(sanitized):
        return False
    first_word = sanitized.split()[0].upper() if sanitized.split() else ""
    return first_word in ("SELECT", "WITH")


def _ensure_limit(sql: str, max_rows: int = 100) -> str:
    sanitized = _sanitize_sql(sql).rstrip(";")
    if "LIMIT" not in sanitized.upper():
        return f"{sanitized} LIMIT {max_rows}"
    return sql


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
)
def list_data_tables() -> str:
    username = _resolve_username()
    from qwenpaw.data_tables.store import _get_conn, _put_conn

    conn = _get_conn()
    try:
        import psycopg2.extras

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT table_name, description, columns, row_count, initialized
                FROM data_table_registry
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
            }
        },
        "required": ["table_name"],
    },
)
def describe_data_table(table_name: str) -> str:
    username = _resolve_username()
    from qwenpaw.data_tables.store import _get_conn, _put_conn
    from qwenpaw.data_tables.connection import get_target_connection, put_target_connection

    conn = _get_conn()
    try:
        import psycopg2.extras

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM data_table_registry
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
            }
        },
        "required": ["sql"],
    },
)
def query_data_table(sql: str) -> str:
    if not _is_read_only(sql):
        return "安全限制：仅允许 SELECT 查询。不允许 INSERT/UPDATE/DELETE/DROP 等写入操作。"
    username = _resolve_username()
    sql = _ensure_limit(sql, 100)

    from qwenpaw.data_tables.connection import (
        get_target_connection,
        put_target_connection,
        _slug_username,
    )

    hg_conn = get_target_connection(_slug_username(username))
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
        put_target_connection(hg_conn, _slug_username(username))
