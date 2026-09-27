# -*- coding: utf-8 -*-
"""智能体工具：对当前账号自己的关系表做图分析。

场景：用户导入一份通联话单（主叫/被叫/通话次数）或转账流水，想找出关联紧密的
群体、核心成员、以及两个人之间隔着几层关系。这些是**图算法**问题，纯 SQL 做
不好（递归 CTE 在 20 万条话单上要 19.9 秒，且写法略错就会给出错误结果），所以
用 networkx/scipy 在内存里算。

**隔离**：数据从**当前账号自己的** PG 库读取（``data_tables`` 的每账号独立数据库），
与 ``query_data_table`` 走同一套连接与账号解析。因此隔离由存储层保证 —— 不是
在这里做过滤。图谱的规模与列结构都来自用户自己的表，不存在跨账号读取的路径。
"""

from __future__ import annotations

import logging

from ...runtime.tool_registry import tool_descriptor
from ...utils.io_utils import run_sync_io
from .data_tables import _resolve_username

logger = logging.getLogger(__name__)

# 一次分析最多渲染多少项，避免把长列表灌进模型上下文。
_MAX_RENDERED_GROUPS = 15
_MAX_RENDERED_MEMBERS = 30
_MAX_RENDERED_RANKING = 30


def _read_edges(
    *,
    table_name: str,
    source_column: str,
    target_column: str,
    weight_column: str | None,
    where: str | None,
    limit: int,
) -> list[tuple]:
    """从当前账号自己的库里读出关系边。

    列名来自用户输入，因此必须过标识符校验再拼接 —— 表名/列名无法参数化，
    这是 SQL 标识符注入的经典入口。表是否存在、是否属于本账号，由既有
    ``_find_table_sync`` 校验（它按 owner 查询注册表）。
    """
    from qwenpaw.data_tables.connection import (
        ensure_user_database,
        get_target_connection,
        put_target_connection,
    )

    username = _resolve_username()
    db_name = ensure_user_database(username)
    columns = [source_column, target_column]
    if weight_column:
        columns.append(weight_column)
    select_list = ", ".join(f'"{column}"' for column in columns)

    sql = f'SELECT {select_list} FROM "{table_name}"'  # noqa: S608 - 标识符已校验
    if where:
        # 允许用户用 WHERE 缩小范围（大表否则会超内存），但仍是只读校验过的。
        sql += f" WHERE {where}"
    sql += f" LIMIT {int(limit)}"

    conn = get_target_connection(db_name)
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '60s'")
            cur.execute(sql)
            rows = cur.fetchall()
    finally:
        put_target_connection(conn, db_name)
    return rows


def _validate_identifier(value: str, *, label: str) -> str:
    """校验表名/列名只含安全字符，防标识符注入。

    表名与列名无法参数化，只能拼进 SQL，所以必须白名单式校验。这里采用
    **允许列表**而不是黑名单：任何不在「字母/数字/下划线/中文」之外的字符都
    拒绝。黑名单（只挡引号分号）会漏掉空格、换行、反引号、Unicode 引号等
    能改变语句结构的字符 —— 例如 ``a b`` 拼接后会变成两个标识符，``a\\nb``
    可能被某些驱动拆行解析。

    中文允许，因为导入时表头常是中文（如「主叫号码」）。
    """
    from qwenpaw.data_tables.graph_analysis import GraphAnalysisError

    name = (value or "").strip().strip('"')
    if not name:
        raise GraphAnalysisError(f"{label}不能为空。")
    if len(name) > 63:
        raise GraphAnalysisError(f"{label}过长（超过 63 字符）。")
    for ch in name:
        if ch.isalnum() or ch == "_":
            continue
        raise GraphAnalysisError(
            f"{label}包含非法字符 {ch!r}：{value!r}。"
            "只能使用字母、数字、下划线和中文。",
        )
    return name


def _run_analysis(
    *,
    table_name: str,
    source_column: str,
    target_column: str,
    weight_column: str | None,
    algorithm: str,
    source: str | None,
    target: str | None,
    top_n: int,
    where: str | None,
    limit: int,
) -> str:
    """同步执行全流程：读边 → 建图 → 算 → 渲染。"""
    from qwenpaw.data_tables import graph_analysis as ga

    table_name = _validate_identifier(table_name, label="表名")
    source_column = _validate_identifier(source_column, label="起点列名")
    target_column = _validate_identifier(target_column, label="终点列名")
    if weight_column:
        weight_column = _validate_identifier(weight_column, label="权重列名")

    rows = _read_edges(
        table_name=table_name,
        source_column=source_column,
        target_column=target_column,
        weight_column=weight_column,
        where=where,
        limit=limit,
    )
    if not rows:
        return (
            f"表 `{table_name}` 里没有读到关系记录"
            + (f"（筛选条件：{where}）" if where else "")
            + "。请确认表已导入数据，或放宽筛选条件。"
        )

    edges = ga.normalize_edges(rows)
    if not edges:
        return (
            f"表 `{table_name}` 里没有可用的关系记录"
            "（全部是自己打给自己这类自环记录，不构成关系）。"
        )

    graph = ga.build_graph(edges)
    node_count = graph.number_of_nodes()
    edge_count = graph.number_of_edges()

    header = (
        f"图分析（表 `{table_name}`，{source_column} → {target_column}"
        + (f"，权重 {weight_column}" if weight_column else "")
        + f"）：{node_count:,} 个节点，{edge_count:,} 条关系。"
    )

    if algorithm == "connected_components":
        groups = ga.connected_components(graph)
        multi = [group for group in groups if len(group) > 1]
        if not multi:
            return header + "\n\n没有两个人之间存在关联（全是孤立号码）。"
        lines = [header, ""]
        lines.append(
            f"共 {len(multi)} 个关联群组（成员数 > 1），"
            f"最大 {len(multi[0])} 人。下面是最大的 {min(len(multi), _MAX_RENDERED_GROUPS)} 个：",
        )
        for index, group in enumerate(multi[:_MAX_RENDERED_GROUPS], 1):
            shown = group[:_MAX_RENDERED_MEMBERS]
            suffix = (
                f" … 等 {len(group)} 人"
                if len(group) > len(shown)
                else ""
            )
            lines.append(
                f"{index}. （{len(group)} 人）{', '.join(shown)}{suffix}",
            )
        if len(multi) > _MAX_RENDERED_GROUPS:
            lines.append(
                f"\n（另有 {len(multi) - _MAX_RENDERED_GROUPS} 个较小的群组未列出）",
            )
        return "\n".join(lines)

    if algorithm == "louvain":
        groups = ga.louvain_communities(graph)
        multi = [group for group in groups if len(group) > 1]
        if not multi:
            return header + "\n\n没有形成任何多人社区。"
        lines = [header, ""]
        lines.append(
            f"按联系紧密程度划分出 {len(multi)} 个团伙，最大 {len(multi[0])} 人。"
            f"下面是最大的 {min(len(multi), _MAX_RENDERED_GROUPS)} 个：",
        )
        for index, group in enumerate(multi[:_MAX_RENDERED_GROUPS], 1):
            shown = group[:_MAX_RENDERED_MEMBERS]
            suffix = f" … 等 {len(group)} 人" if len(group) > len(shown) else ""
            lines.append(f"{index}. （{len(group)} 人）{', '.join(shown)}{suffix}")
        if len(multi) > _MAX_RENDERED_GROUPS:
            lines.append(
                f"\n（另有 {len(multi) - _MAX_RENDERED_GROUPS} 个较小的团伙未列出）",
            )
        lines.append(
            "\n说明：社区发现按联系密度切分，比「能连上就算一群」更接近真实团伙；"
            "同一份数据每次结果一致。",
        )
        return "\n".join(lines)

    if algorithm in ("degree", "weighted_degree", "pagerank"):
        method = {
            "degree": "degree",
            "weighted_degree": "weighted_degree",
            "pagerank": "pagerank",
        }[algorithm]
        ranking = ga.rank_nodes(graph, method=method, top_n=top_n)
        labels = {
            "degree": "对端数（联系的人最多）",
            "weighted_degree": "关系总量（联系最频繁）",
            "pagerank": "网络核心度（处于关系网枢纽）",
        }[method]
        lines = [header, "", f"关联度排名（口径：{labels}）："]
        for index, (node, score) in enumerate(
            ranking[:_MAX_RENDERED_RANKING], 1,
        ):
            if method == "pagerank":
                lines.append(f"{index}. {node}  {score:.4f}")
            else:
                lines.append(f"{index}. {node}  {score:g}")
        return "\n".join(lines)

    if algorithm == "shortest_path":
        if not source or not target:
            return "查询关系链需要同时提供 source 和 target（起止号码）。"
        path, hops = ga.shortest_path(graph, source, target)
        lines = [header, ""]
        if hops == 0:
            lines.append(f"{source} 与 {target} 是同一个节点。")
        else:
            lines.append(f"{source} 到 {target} 的最短关系链：{hops} 层")
            lines.append(" → ".join(path))
        return "\n".join(lines)

    return (
        f"不支持的算法 {algorithm!r}。可选：connected_components（关联群组）、"
        "louvain（团伙划分）、degree / weighted_degree / pagerank（关联度排序）、"
        "shortest_path（关系链）。"
    )


@tool_descriptor(
    name="analyze_graph",
    description=(
        "对当前用户业务表中的**关系数据**做图分析。适用于通联话单、转账流水、"
        "共同出现关系等「谁和谁有关联」的场景。\n"
        "支持四种分析：\n"
        "- connected_components：找出所有互相连通的群体（谁和谁在一个圈子里）\n"
        "- louvain：按联系紧密程度划分团伙（比连通分量更接近真实团伙）\n"
        "- degree / weighted_degree / pagerank：关联度排名（找核心成员、话务枢纽）\n"
        "- shortest_path：两个人之间隔着几层关系、中间经过谁\n"
        "数据取自当前用户自己的数据表，不会访问其他用户的数据。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "table_name": {
                "type": "string",
                "description": "关系数据所在的表名（当前用户的数据表）",
            },
            "source_column": {
                "type": "string",
                "description": "关系起点列名，例如主叫号码列",
            },
            "target_column": {
                "type": "string",
                "description": "关系终点列名，例如被叫号码列",
            },
            "weight_column": {
                "type": "string",
                "description": (
                    "可选。关系强度列名，例如通话次数、转账金额。"
                    "提供后关联度与社区划分会考虑强度；不提供则每条关系等权。"
                ),
            },
            "algorithm": {
                "type": "string",
                "enum": [
                    "connected_components",
                    "louvain",
                    "degree",
                    "weighted_degree",
                    "pagerank",
                    "shortest_path",
                ],
                "description": "要执行的分析",
            },
            "source": {
                "type": "string",
                "description": "algorithm=shortest_path 时的起点号码",
            },
            "target": {
                "type": "string",
                "description": "algorithm=shortest_path 时的终点号码",
            },
            "top_n": {
                "type": "integer",
                "description": "关联度排名的返回条数，默认 20",
            },
            "where": {
                "type": "string",
                "description": (
                    "可选。只读筛选条件（SQL WHERE 片段，不含 WHERE 关键字），"
                    "例如 `通话日期 >= '2026-01-01'`，用于缩小分析范围。"
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "最多读取多少条关系记录，默认 500000。"
                    "数据量很大时可用 where 先筛选。"
                ),
            },
        },
        "required": ["table_name", "source_column", "target_column", "algorithm"],
    },
    tool_type="internal",
    policy_name="AnalyzeGraph",
    default_policy="allow",
    policy_reason=(
        "Read-only graph analysis scoped to the current user's own database"
    ),
)
async def analyze_graph(
    table_name: str,
    source_column: str,
    target_column: str,
    algorithm: str,
    weight_column: str | None = None,
    source: str | None = None,
    target: str | None = None,
    top_n: int = 20,
    where: str | None = None,
    limit: int = 500_000,
) -> str:
    from qwenpaw.data_tables.graph_analysis import GraphAnalysisError

    # 读库与算法都是阻塞型（CSV 级读取 + 纯 Python 迭代），必须离开事件循环，
    # 否则 Louvain 这类秒级计算会卡住整个会话。
    try:
        return await run_sync_io(
            _run_analysis,
            table_name=table_name,
            source_column=source_column,
            target_column=target_column,
            weight_column=weight_column,
            algorithm=algorithm,
            source=source,
            target=target,
            top_n=int(top_n or 20),
            where=where,
            limit=int(limit or 500_000),
        )
    except GraphAnalysisError as exc:
        # 面向用户的输入/规模问题：给可读提示，不是异常。
        return f"图分析未完成：{exc}"
    except Exception as exc:  # noqa: BLE001 - 工具失败不应中断对话
        logger.warning("analyze_graph failed", exc_info=True)
        return f"图分析执行失败：{type(exc).__name__}: {exc}"


__all__ = ["analyze_graph"]
