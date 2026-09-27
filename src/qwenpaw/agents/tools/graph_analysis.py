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

# 从算法模块取清单，避免「这里写一份、那里写一份」两处漂移。
from qwenpaw.data_tables.graph_analysis import (  # noqa: E402
    ALGORITHMS as _ALGORITHMS,
)

_SUPPORTED_ALGORITHMS = tuple(_ALGORITHMS)
_ALGORITHM_HELP = "要执行的分析：\n" + "\n".join(
    f"- {name}（{_ALGORITHMS[name].tier}）：{_ALGORITHMS[name].description}"
    for name in _ALGORITHMS
)
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
    radius: int,
    min_size: int,
    max_length: int,
    predict_method: str,
    k: int,
) -> str:
    """同步执行全流程：读边 → 建图 → 按算法算 → 渲染。"""
    from qwenpaw.data_tables import graph_analysis as ga

    table_name = _validate_identifier(table_name, label="表名")
    source_column = _validate_identifier(source_column, label="起点列名")
    target_column = _validate_identifier(target_column, label="终点列名")
    if weight_column:
        weight_column = _validate_identifier(weight_column, label="权重列名")

    if algorithm not in ga.ALGORITHMS:
        return (
            f"不支持的算法 {algorithm!r}。可用算法：\n"
            + "\n".join(
                f"- {name}（{ga.ALGORITHMS[name].tier}）："
                f"{ga.ALGORITHMS[name].description}"
                for name in ga.ALGORITHMS
            )
        )

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
    # 规模校验在算法执行之前 —— 等算完才发现太慢，用户已经等了几分钟。
    ga.check_scale(algorithm, graph)

    node_count = graph.number_of_nodes()
    edge_count = graph.number_of_edges()
    header = (
        f"图分析（表 `{table_name}`，{source_column} → {target_column}"
        + (f"，权重 {weight_column}" if weight_column else "")
        + f"）：{node_count:,} 个节点，{edge_count:,} 条关系。"
    )

    if algorithm == "connected_components":
        return _render_groups(
            header,
            ga.connected_components(graph),
            noun="关联群组",
            empty="没有两个人之间存在关联（全是孤立号码）。",
        )
    if algorithm == "louvain":
        return _render_groups(
            header,
            ga.louvain_communities(graph),
            noun="团伙",
            empty="没有形成任何多人社区。",
            note="按联系密度切分，比「能连上就算一群」更接近真实团伙；结果稳定可复核。",
        )
    if algorithm == "label_propagation":
        return _render_groups(
            header,
            ga.label_propagation_communities(graph),
            noun="社区",
            empty="没有形成任何多人社区。",
            note=(
                "标签传播比 Louvain 快得多，但结果**不保证每次一致** —— "
                "需要可复核的划分请改用 louvain。"
            ),
        )
    if algorithm == "k_components":
        groups, level = ga.k_components_with_level(graph, k=k)
        if not groups:
            return (
                header
                + f"\n\n没有 {k}-连通的群组"
                "（没有「去掉若干成员后仍完整」的紧密群体）。"
                "可降低 k 试试，或改用 louvain 看按密度划分的结果。"
            )
        # 请求的层级上可能没有群组，此时会退到实际最深层；如实说明层级，
        # 否则会把「这是 2-连通」说成「3-连通」。
        strengths = ga.group_connectivity(graph, groups)
        # 层级受请求的 k 影响（k_components 会把最高层重复填到更高层级），
        # 所以结论用实测连通度表述，而不是照抄 k —— 否则会低估紧密程度。
        shown = [
            f"{len(group)} 人，连通度 {strength}" if strength else f"{len(group)} 人"
            for group, strength in zip(groups, strengths)
        ]
        lines = [header, ""]
        lines.append(
            f"共 {len(groups)} 个紧密群组（成员数 > 1），"
            f"下面是最大的 {min(len(groups), _MAX_RENDERED_GROUPS)} 个：",
        )
        for index, (group, label) in enumerate(
            zip(groups, shown), 1,
        ):
            if index > _MAX_RENDERED_GROUPS:
                break
            members = group[:_MAX_RENDERED_MEMBERS]
            suffix = f" … 等 {len(group)} 人" if len(group) > len(members) else ""
            lines.append(
                f"{index}. （{label}）{', '.join(members)}{suffix}",
            )
        if len(groups) > _MAX_RENDERED_GROUPS:
            lines.append(f"\n（另有 {len(groups) - _MAX_RENDERED_GROUPS} 个较小的未列出）")
        lines.append(
            "\n说明：连通度 = 至少去掉多少个成员才会让这个群组散开，数值越高越紧密。"
            "它比连通分量严格 —— 「仅仅能连上」不等于「结构紧密」。"
        )
        return "\n".join(lines)

    if algorithm in (
        "degree_centrality",
        "weighted_degree",
        "pagerank",
        "eigenvector_centrality",
        "betweenness_centrality",
        "closeness_centrality",
    ):
        method = {
            "degree_centrality": "degree",
            "weighted_degree": "weighted_degree",
            "pagerank": "pagerank",
            "eigenvector_centrality": "eigenvector",
            "betweenness_centrality": "betweenness",
            "closeness_centrality": "closeness",
        }[algorithm]
        labels = {
            "degree": "对端数（联系的人最多）",
            "weighted_degree": "关系总量（联系最频繁）",
            "pagerank": "网络核心度（处于关系网枢纽）",
            "eigenvector": "特征向量中心性（与重要节点相连）",
            "betweenness": "中介作用（不同圈子之间的中间人）",
            "closeness": "接近中心性（到达所有人最快）",
        }[method]
        ranking = ga.rank_nodes(graph, method=method, top_n=top_n)
        extra = ""
        if method == "betweenness":
            extra = (
                f"\n（介数中心性按 {ga.BETWEENNESS_SAMPLE_SIZE} 个源点采样近似 —— "
                "精确计算在十万级关系上需要十几分钟。）"
            )
        return _render_ranking(header, ranking, labels, method, extra)

    if algorithm == "clustering":
        ranked = ga.clustering_coefficient(graph)
        if not ranked:
            return header + "\n\n没有度数 ≥ 2 的节点，聚集系数无意义。"
        lines = [
            header, "",
            "聚集系数排名（联系人之间是否也互相认识；越接近 1 越像小圈子成员）：",
        ]
        for index, (node, score) in enumerate(ranked[:_MAX_RENDERED_RANKING], 1):
            lines.append(f"{index}. {node}  {score:.4f}")
        return "\n".join(lines)

    if algorithm == "core_number":
        ranked = ga.core_numbers(graph)
        if not ranked:
            return header + "\n\n图中没有节点。"
        lines = [header, "", "k-core 层级（越高表示所处结构越紧密）："]
        for index, (node, value) in enumerate(ranked[:_MAX_RENDERED_RANKING], 1):
            lines.append(f"{index}. {node}  层级 {value}")
        return "\n".join(lines)

    if algorithm in ("articulation_points", "bridges"):
        kind = algorithm
        found = ga.key_nodes(graph, kind=kind)
        if not found:
            noun = "关键联络人（割点）" if kind == "articulation_points" else "唯一联系方式（桥）"
            return (
                header
                + f"\n\n没有找到{noun}—— 说明网络中没有「去掉就会分裂」的单点。"
            )
        if kind == "articulation_points":
            lines = [
                header, "",
                f"关键联络人（去掉它网络就会分裂），共 {len(found)} 个：",
            ]
            for index, node in enumerate(found[:_MAX_RENDERED_RANKING], 1):
                lines.append(f"{index}. {node}")
        else:
            lines = [
                header, "",
                f"唯一联系方式（去掉这条关系网络就会分裂），共 {len(found)} 条：",
            ]
            for index, pair in enumerate(found[:_MAX_RENDERED_RANKING], 1):
                left, right = pair.split("|", 1)
                lines.append(f"{index}. {left} ↔ {right}")
        if len(found) > _MAX_RENDERED_RANKING:
            lines.append(f"\n（另有 {len(found) - _MAX_RENDERED_RANKING} 个未列出）")
        return "\n".join(lines)

    if algorithm == "shortest_path":
        if not source or not target:
            return "查询关系链需要同时提供 source 和 target（起止号码）。"
        path, hops = ga.shortest_path(graph, source, target)
        if hops == 0:
            return header + f"\n\n{source} 与 {target} 是同一个节点。"
        return (
            header
            + f"\n\n{source} 到 {target} 的最短关系链：{hops} 层\n"
            + " → ".join(path)
        )

    if algorithm == "all_paths":
        if not source or not target:
            return "查询全部路径需要同时提供 source 和 target。"
        paths = ga.all_simple_paths(graph, source, target, limit=top_n)
        lines = [
            header, "",
            f"{source} 到 {target} 的路径（按长度排序，最多 {top_n} 条）共 {len(paths)} 条：",
        ]
        for index, path in enumerate(paths, 1):
            lines.append(f"{index}. （{len(path) - 1} 层）{' → '.join(path)}")
        if len(paths) >= top_n:
            lines.append(f"\n（结果达到 {top_n} 条上限，可能还有更多路径）")
        return "\n".join(lines)

    if algorithm == "link_prediction":
        pairs = ga.predict_links(
            graph, method=predict_method, limit=top_n,
        )
        if not pairs:
            return (
                header
                + "\n\n没有找到可能认识但尚未直连的节点对"
                "（候选集合为空，或不存在的共同联系人）。"
            )
        labels = {
            "jaccard": "Jaccard（共同联系人占比）",
            "adamic_adar": "Adamic-Adar（共同联系人越冷门权重越高）",
            "resource_allocation": "资源分配（对冷门联系人更敏感）",
        }[predict_method]
        lines = [
            header, "",
            f"很可能认识但数据里尚无直接关系的节点对（口径：{labels}）：",
        ]
        for index, (left, right, score) in enumerate(pairs, 1):
            lines.append(f"{index}. {left} ↔ {right}  分值 {score:.4f}")
        lines.append(
            "\n分值越高表示共同联系人越多、越可能存在未记录的关系。",
        )
        return "\n".join(lines)

    if algorithm == "cliques":
        if not source:
            return "团分析需要提供 source（以谁为中心在其邻域内枚举）。"
        cliques = ga.find_cliques(
            graph, center=source, radius=radius, min_size=min_size, limit=top_n,
        )
        if not cliques:
            return (
                header
                + f"\n\n{source} 的 {radius} 跳邻域内没有规模 ≥ {min_size} 的团。"
            )
        lines = [
            header, "",
            f"{source} 的 {radius} 跳邻域内互为直接关系的小团体"
            f"（规模 ≥ {min_size}），共 {len(cliques)} 个：",
        ]
        for index, group in enumerate(cliques, 1):
            lines.append(f"{index}. （{len(group)} 人）{', '.join(group)}")
        return "\n".join(lines)

    if algorithm == "cycles":
        if not source:
            return "环分析需要提供 source（以谁为中心在其邻域内枚举）。"
        cycles = ga.find_cycles(
            graph, center=source, radius=radius,
            max_length=max_length, limit=top_n,
        )
        if not cycles:
            return (
                header
                + f"\n\n{source} 的 {radius} 跳邻域内没有长度 ≤ {max_length} 的环。"
            )
        lines = [
            header, "",
            f"{source} 邻域内的关系环（长度 ≤ {max_length}），共 {len(cycles)} 个：",
        ]
        for index, cycle in enumerate(cycles, 1):
            # 环首尾相接，展示时闭合起来更易读。
            lines.append(f"{index}. （{len(cycle)} 环）{' → '.join(cycle)} → {cycle[0]}")
        return "\n".join(lines)

    return f"算法 {algorithm} 已通过校验但未实现渲染，请反馈此问题。"


def _render_groups(
    header: str,
    groups: list[list[str]],
    *,
    noun: str,
    empty: str,
    note: str = "",
) -> str:
    """渲染「分组」类结果（连通分量 / 社区 / 团伙）。"""
    multi = [group for group in groups if len(group) > 1]
    if not multi:
        return header + "\n\n" + empty
    lines = [header, ""]
    lines.append(
        f"共 {len(multi)} 个{noun}（成员数 > 1），最大 {len(multi[0])} 人。"
        f"下面是最大的 {min(len(multi), _MAX_RENDERED_GROUPS)} 个：",
    )
    for index, group in enumerate(multi[:_MAX_RENDERED_GROUPS], 1):
        shown = group[:_MAX_RENDERED_MEMBERS]
        suffix = f" … 等 {len(group)} 人" if len(group) > len(shown) else ""
        lines.append(f"{index}. （{len(group)} 人）{', '.join(shown)}{suffix}")
    if len(multi) > _MAX_RENDERED_GROUPS:
        lines.append(f"\n（另有 {len(multi) - _MAX_RENDERED_GROUPS} 个较小的未列出）")
    if note:
        lines.append(f"\n说明：{note}")
    return "\n".join(lines)


def _render_ranking(
    header: str,
    ranking: list[tuple[str, float]],
    label: str,
    method: str,
    extra: str,
) -> str:
    """渲染「排名」类结果（各种中心性）。"""
    lines = [header, "", f"关联度排名（口径：{label}）："]
    for index, (node, score) in enumerate(
        ranking[:_MAX_RENDERED_RANKING], 1,
    ):
        if method in ("pagerank", "eigenvector", "betweenness", "closeness"):
            lines.append(f"{index}. {node}  {score:.4f}")
        else:
            lines.append(f"{index}. {node}  {score:g}")
    if extra:
        lines.append(extra)
    return "\n".join(lines)


@tool_descriptor(
    name="analyze_graph",
    description=(
        "对当前用户业务表中的**关系数据**做图分析。适用于通联话单、转账流水、"
        "共同出现关系等「谁和谁有关联」的场景。\n\n"
        "可用算法（按成本分档；light 可即时返回，heavy 需较大数据量前先筛选，"
        "extreme 默认拒绝并要求先缩小范围）：\n"
        + _ALGORITHM_HELP
        + "\n\n数据取自当前用户自己的数据表，不会访问其他用户的数据。"
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
                # 与 graph_analysis.ALGORITHMS 保持一致（单一事实来源）。
                "enum": list(_SUPPORTED_ALGORITHMS),
                "description": _ALGORITHM_HELP,
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
                "description": (
                    "返回条数上限（排名/路径/团/环/预测），默认 20"
                ),
            },
            "radius": {
                "type": "integer",
                "description": (
                    "algorithm=cliques/cycles 时，以 source 为中心向外扩展几跳"
                    "（默认 2）。团与环只在邻域内枚举，全图枚举不会收敛。"
                ),
            },
            "min_size": {
                "type": "integer",
                "description": "algorithm=cliques 时的最小团规模，默认 3",
            },
            "max_length": {
                "type": "integer",
                "description": "algorithm=cycles 时的最大环长，默认 5",
            },
            "predict_method": {
                "type": "string",
                "enum": ["jaccard", "adamic_adar", "resource_allocation"],
                "description": (
                    "algorithm=link_prediction 的打分口径，默认 jaccard"
                ),
            },
            "k": {
                "type": "integer",
                "description": "algorithm=k_components 时的 k 值，默认 3",
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
    radius: int = 2,
    min_size: int = 3,
    max_length: int = 5,
    predict_method: str = "jaccard",
    k: int = 3,
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
            radius=int(radius or 2),
            min_size=int(min_size or 3),
            max_length=int(max_length or 5),
            predict_method=predict_method or "jaccard",
            k=int(k or 3),
        )
    except GraphAnalysisError as exc:
        # 面向用户的输入/规模问题：给可读提示，不是异常。
        return f"图分析未完成：{exc}"
    except Exception as exc:  # noqa: BLE001 - 工具失败不应中断对话
        logger.warning("analyze_graph failed", exc_info=True)
        return f"图分析执行失败：{type(exc).__name__}: {exc}"


__all__ = ["analyze_graph"]
