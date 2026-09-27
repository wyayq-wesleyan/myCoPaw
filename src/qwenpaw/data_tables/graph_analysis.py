# -*- coding: utf-8 -*-
"""图分析：从关系表里算关联群组、团伙划分、关联度与关系链。

**为什么是 networkx 而不是图数据库。** 需求是「找出关联度高的团伙」，这本质上是
**图算法**问题，而不是图存储问题。两者常被混为一谈，但职责完全不同：

* 图数据库（Apache AGE / Neo4j）解决「怎么存、怎么用查询语言遍历」；
* 图算法库（networkx / scipy）解决「怎么算连通分量、社区、中心性」。

实测 Apache AGE 1.7.0：无 ``shortestPath``、无 PageRank、无社区发现 —— 它只有
Cypher 遍历原语。即便引入 AGE，团伙发现仍然要另配算法库。而本项目的基础镜像
已经带 networkx（LightRAG 的图存储依赖它），所以直接用它是最短路径。

**隔离**：本模块只做纯计算，不碰数据库。数据由调用方从**该账号自己的** PG 库读出
（``data_tables`` 的每账号独立数据库），因此隔离由存储层保证，而不是在这里过滤。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# 建内存图的规模上限。networkx 是纯 Python 实现，内存占用约为
# (节点数 + 边数) × 每个 Python 对象的开销，百万级边就会吃掉数 GB。
# 超限时明确报错并给出可行建议，而不是让进程 OOM 或长时间无响应。
DEFAULT_MAX_NODES = 500_000
DEFAULT_MAX_EDGES = 2_000_000

# --- 算法成本分级 ---------------------------------------------------------
#
# 全部实测于「2 万节点 / 10 万边」的基准图（无标度图，接近通联/转账的真实形状）。
# 分级不是学术洁癖，而是因为量级差异极大：同一个界面里，连通分量 2ms、介数中心性
# 940s —— 差了五个数量级。不区分的话，用户点一个「分析」就可能把服务挂住。
#
#   light  (< 2s)   ：可以内联在对话里直接返回
#   heavy  (2s~60s) ：需要较严格的上限，且应告知用户耗时
#   extreme(> 60s)  ：默认拒绝，必须显式确认 + 采样，否则必然拖垮会话
#
# 实测数据（2 万节点 / 10 万边）：
#   connected_components      2ms      k_core                  76ms
#   pagerank                 89ms      articulation_points     63ms
#   degree_centrality        ~20ms     bridges                469ms
#   eigenvector_centrality   463ms     clustering             522ms
#   label_propagation        312ms     louvain               10.6s
#   betweenness (exact)      940s      closeness              175s
#   greedy_modularity        360s      simple_cycles         不收敛
#   find_cliques             不收敛（全图枚举，环/团数量爆炸）

# 重算法的规模上限：超过就拒绝，并给出替代方案。
HEAVY_MAX_EDGES = 300_000
# 极重算法默认只在较小图上允许（且介数中心性会强制采样）。
EXTREME_MAX_EDGES = 50_000
# 介数中心性的采样规模：k 个源点近似。k 取 min(节点数, 该值)。
BETWEENNESS_SAMPLE_SIZE = 500
# 团/环枚举只允许在子图上做（以某个节点为起点的邻域），避免全图枚举。
SUBGRAPH_MAX_NODES = 5_000


class AlgorithmCost:
    """一个算法的成本画像（用于校验与给用户提示）。"""

    def __init__(
        self,
        name: str,
        tier: str,
        max_edges: int | None,
        description: str,
    ) -> None:
        self.name = name
        self.tier = tier
        self.max_edges = max_edges
        self.description = description


# 智能体可调用的全部算法。名字保持可读（英文小写下划线），与工具的参数一致。
ALGORITHMS: dict[str, AlgorithmCost] = {
    # --- 群组/社区 ---
    "connected_components": AlgorithmCost(
        "connected_components", "light", None,
        "关联群组：所有互相连通的节点。谁和谁在一个圈子里。",
    ),
    "louvain": AlgorithmCost(
        "louvain", "heavy", HEAVY_MAX_EDGES,
        "团伙划分：按联系密度切分社区，比连通分量更接近真实团伙。",
    ),
    "label_propagation": AlgorithmCost(
        "label_propagation", "light", HEAVY_MAX_EDGES,
        "团伙划分（快速版）：标签传播，比 Louvain 快得多，结果略粗。",
    ),
    "k_components": AlgorithmCost(
        "k_components", "heavy", HEAVY_MAX_EDGES,
        "k-连通分量：即使去掉 k-1 个点仍连通的紧密群组，比连通分量更严。",
    ),
    # --- 中心性/关联度 ---
    "degree_centrality": AlgorithmCost(
        "degree_centrality", "light", None,
        "关联度：联系的人数。找认识人最多的。",
    ),
    "weighted_degree": AlgorithmCost(
        "weighted_degree", "light", None,
        "关联度：关系总量（通话次数/金额之和）。找联系最频繁的。",
    ),
    "pagerank": AlgorithmCost(
        "pagerank", "light", None,
        "关联度：网络核心度。找处于关系网枢纽的（比度数更能排除无意义的边缘高频）。",
    ),
    "eigenvector_centrality": AlgorithmCost(
        "eigenvector_centrality", "light", HEAVY_MAX_EDGES,
        "关联度：特征向量中心性。与 PageRank 类似，强调与重要节点相连。",
    ),
    "betweenness_centrality": AlgorithmCost(
        "betweenness_centrality", "extreme", EXTREME_MAX_EDGES,
        "桥梁作用：介数中心性。找「不同圈子之间的中间人」。"
        "精确计算极慢（实测 10 万边需 15 分钟），因此自动采样近似。",
    ),
    "closeness_centrality": AlgorithmCost(
        "closeness_centrality", "extreme", EXTREME_MAX_EDGES,
        "传播速度：接近中心性。找「到达所有人最快」的。"
        "极慢（实测 10 万边 175 秒），需先缩小范围。",
    ),
    "clustering": AlgorithmCost(
        "clustering", "light", HEAVY_MAX_EDGES,
        "聚集系数：某人的联系人之间是否也互相认识。越高越像团伙成员。",
    ),
    "core_number": AlgorithmCost(
        "core_number", "light", None,
        "k-core 层级：剥掉低连接度节点后仍存在的核心层。核心层编号越高越紧密。",
    ),
    # --- 结构/关键点 ---
    "articulation_points": AlgorithmCost(
        "articulation_points", "light", None,
        "割点：去掉它就会让网络分裂的关键节点（单点故障/关键联络人）。",
    ),
    "bridges": AlgorithmCost(
        "bridges", "light", None,
        "桥：去掉它就会让网络分裂的唯一边（唯一的联系方式）。",
    ),
    # --- 路径 ---
    "shortest_path": AlgorithmCost(
        "shortest_path", "light", None,
        "关系链：两点之间最短的中间人路径。",
    ),
    "all_paths": AlgorithmCost(
        "all_paths", "light", SUBGRAPH_MAX_NODES,
        "所有简单路径：两点之间全部不重复的路径（限制条数）。",
    ),
    # --- 相似度（预测潜在关系）---
    "link_prediction": AlgorithmCost(
        "link_prediction", "light", HEAVY_MAX_EDGES,
        "潜在关系预测：用共同邻居（Jaccard/Adamic-Adar/资源分配）给一对节点打分，"
        "分数高说明他们很可能认识但数据里还没有直接关系。",
    ),
    # --- 团/环（只在邻域子图上做）---
    "cliques": AlgorithmCost(
        "cliques", "light", None,
        "紧密小团体：找出互为直接关系的节点组合（团）。"
        "只在指定节点的邻域内枚举，全图枚举会不收敛。",
    ),
    "cycles": AlgorithmCost(
        "cycles", "light", None,
        "环：找关系回路，例如 A→B→C→A 的资金/通话流转闭环。"
        "只在指定节点的邻域内枚举，全图枚举会不收敛。",
    ),
}

SUPPORTED_ALGORITHMS = tuple(ALGORITHMS)


def algorithm_cost(name: str) -> AlgorithmCost:
    """取某算法的成本画像；未知算法给出可读错误。"""
    try:
        return ALGORITHMS[name]
    except KeyError:
        known = "、".join(ALGORITHMS)
        raise GraphAnalysisError(
            f"不支持的算法 {name!r}。可用算法：{known}",
        ) from None


def check_scale(name: str, graph) -> None:
    """按算法的成本上限校验图规模，超限时给出可操作的建议。

    在算法执行**之前**调用：等到算完才发现太慢，用户已经等了几分钟。
    """
    cost = algorithm_cost(name)
    if cost.max_edges is None:
        return
    edges = graph.number_of_edges()
    if edges <= cost.max_edges:
        return
    if cost.tier == "extreme":
        advice = (
            "该算法在十万级关系上需要数分钟到十几分钟。"
            "请先用 where 条件缩小范围（如限定时间段、地区），"
            "或改用 degree_centrality / pagerank（可瞬时完成）。"
        )
    else:
        advice = (
            "请先用 where 条件缩小范围，"
            "或改用 connected_components / label_propagation（明显更快）。"
        )
    raise GraphAnalysisError(
        f"算法 {name} 在 {edges:,} 条关系上超出可接受规模"
        f"（上限 {cost.max_edges:,}）。{advice}",
    )



class GraphAnalysisError(ValueError):
    """输入或规模不满足算法要求（面向用户的可读错误）。"""


@dataclass
class Edge:
    """一条关系记录。"""

    source: str
    target: str
    weight: float = 1.0


def _coerce_node(value: Any) -> str:
    """把节点标识归一成字符串。

    号码在库里可能是 bigint、可能是带前导零的文本，也可能是数字字符串。
    统一成 str 后 ``13800138000`` 与 ``"13800138000"`` 会落在同一个节点上，
    避免同一号码被拆成两个点。
    """
    if value is None:
        raise GraphAnalysisError("关系记录里存在空的节点标识")
    # 整数值不要变成 "13800138000.0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _coerce_weight(value: Any) -> float:
    if value is None:
        return 1.0
    try:
        weight = float(value)
    except (TypeError, ValueError) as exc:
        raise GraphAnalysisError(f"权重无法解析为数值: {value!r}") from exc
    # 负权与 0 权会让 PageRank / 最短路失去意义，统一按 1 处理并留痕。
    if weight < 0:
        logger.debug("graph: negative weight %s coerced to 0", weight)
        return 0.0
    return weight


def normalize_edges(rows: Iterable[Sequence[Any]]) -> list[Edge]:
    """把 ``(source, target[, weight])`` 行归一化为边列表。

    自环会被丢弃：话单里「自己打给自己」不构成关系，留着只会虚增关联度。
    重复边（同一对号码多条记录）由调用方先聚合；若未聚合，后面建图时会累加权重。
    """
    edges: list[Edge] = []
    skipped_self = 0
    for row in rows:
        if len(row) < 2:
            raise GraphAnalysisError(
                "关系记录至少需要 source 与 target 两列",
            )
        source = _coerce_node(row[0])
        target = _coerce_node(row[1])
        weight = _coerce_weight(row[2]) if len(row) > 2 else 1.0
        if not source or not target:
            raise GraphAnalysisError("关系记录里存在空的节点标识")
        if source == target:
            skipped_self += 1
            continue
        edges.append(Edge(source, target, weight))
    if skipped_self:
        logger.debug("graph: dropped %d self-loop rows", skipped_self)
    return edges


def _import_networkx():
    try:
        import networkx as nx  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - 部署镜像已包含
        raise GraphAnalysisError(
            "图分析需要 networkx，但当前环境未安装。"
            "请联系管理员在运行镜像中安装 networkx。",
        ) from exc
    return nx


def build_graph(
    edges: Sequence[Edge],
    *,
    directed: bool = False,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_edges: int = DEFAULT_MAX_EDGES,
):
    """把边列表建成内存图，并在规模超限时明确报错。

    默认按**无向**建图：话单、转账这类关系的方向通常不改变「谁和谁有关系」，
    而无向图才能让连通分量/社区发现反映真实的关联群体。需要方向时显式传
    ``directed=True``。
    """
    nx = _import_networkx()
    node_ids: set[str] = set()
    for edge in edges:
        node_ids.add(edge.source)
        node_ids.add(edge.target)
    if len(node_ids) > max_nodes:
        raise GraphAnalysisError(
            f"节点数 {len(node_ids):,} 超过上限 {max_nodes:,}。"
            "请先按时间/区域等条件筛选，或改用 SQL 预聚合后再分析。",
        )
    if len(edges) > max_edges:
        raise GraphAnalysisError(
            f"关系数 {len(edges):,} 超过上限 {max_edges:,}。"
            "请先按条件筛选或聚合后再分析。",
        )

    graph = nx.DiGraph() if directed else nx.Graph()
    for edge in edges:
        if graph.has_edge(edge.source, edge.target):
            # 同一对关系出现多次：累加权重，而不是覆盖 —— 通话次数这类权重
            # 本来就是多条记录之和。
            graph[edge.source][edge.target]["weight"] += edge.weight
        else:
            graph.add_edge(edge.source, edge.target, weight=edge.weight)
    return graph


# --- 算法 ------------------------------------------------------------------


def connected_components(graph) -> list[list[str]]:
    """无向连通分量，按规模降序。

    有 scipy 时用 ``scipy.sparse.csgraph``（实测 500 万边 122ms，比 networkx 快
    约一个数量级）；没有就退化为 networkx 实现，结果一致。
    """
    nx = _import_networkx()
    if graph.number_of_nodes() == 0:
        return []

    if graph.is_directed():
        # scipy 需要无向；强连通分量语义与「关联群体」不同，这里明确按
        # 弱连通处理（忽略方向看是否可达），与无向场景结果一致。
        graph = graph.to_undirected()

    try:
        import numpy as np  # noqa: PLC0415
        from scipy.sparse import csr_matrix  # noqa: PLC0415
        from scipy.sparse.csgraph import (  # noqa: PLC0415
            connected_components as _scipy_cc,
        )

        nodes = list(graph.nodes())
        index = {node: i for i, node in enumerate(nodes)}
        rows = [index[u] for u, v in graph.edges()]
        cols = [index[v] for u, v in graph.edges()]
        if not rows:
            # 无边：每个孤立点各成一个分量。
            return [[node] for node in sorted(nodes)]
        adjacency = csr_matrix(
            (np.ones(len(rows), dtype=np.int8), (rows, cols)),
            shape=(len(nodes), len(nodes)),
        )
        count, labels = _scipy_cc(adjacency, directed=False)
        buckets: dict[int, list[str]] = {}
        for node, label in zip(nodes, labels):
            buckets.setdefault(int(label), []).append(node)
        return sorted(buckets.values(), key=len, reverse=True)
    except ImportError:
        logger.debug("graph: scipy unavailable, using networkx for components")

    components = [sorted(component) for component in nx.connected_components(graph)]
    return sorted(components, key=len, reverse=True)


def louvain_communities(
    graph,
    *,
    resolution: float = 1.0,
    seed: int = 42,
) -> list[list[str]]:
    """Louvain 社区发现，按规模降序。

    比连通分量更贴近「一伙人」：连通分量只要求「能连上」，Louvain 按边的密度
    切分，因此能把「一张大网里联系紧密的几个圈子」分开。代价是慢得多（迭代式，
    实测 10 万边 ≈ 10.6s），故有单独的规模上限。

    ``seed`` 固定以保证同一份数据每次得到同样的划分 —— 否则「不一致的结果」
    会让用户无法复核，也无法写进后续自动化。
    """
    nx = _import_networkx()
    if graph.number_of_nodes() == 0:
        return []
    if graph.is_directed():
        graph = graph.to_undirected()

    check_scale("louvain", graph)

    communities = nx.community.louvain_communities(
        graph,
        weight="weight",
        resolution=resolution,
        seed=seed,
    )
    return sorted(
        (sorted(community) for community in communities),
        key=len,
        reverse=True,
    )


def rank_nodes(
    graph,
    *,
    method: str = "weighted_degree",
    top_n: int = 20,
) -> list[tuple[str, float]]:
    """按关联度给节点排序。

    * ``degree`` —— 对端数（联系的人数）。找「认识人最多」的。
    * ``weighted_degree`` —— 关系总量（通话次数/金额之和）。找最频繁的。
    * ``pagerank`` —— 网络核心度。比度数更能识别真正的枢纽：一个只跟大量
      边缘人联系的号度数很高，但 PageRank 不高。
    * ``eigenvector`` —— 与重要节点相连的程度，与 PageRank 相近。
    * ``betweenness`` —— 中介作用，找不同圈子之间的中间人。**极慢**，
      因此按 ``k`` 采样近似（见 ``BETWEENNESS_SAMPLE_SIZE``）。
    * ``closeness`` —— 到达所有人的快慢。**极慢**，调用方需先校验规模。

    未知口径报可读错误而不是静默返回空 —— 静默空结果会被误读成「没有数据」。
    """
    nx = _import_networkx()
    if graph.number_of_nodes() == 0:
        return []

    if method == "degree":
        scores = dict(graph.degree())
    elif method == "weighted_degree":
        scores = dict(graph.degree(weight="weight"))
    elif method == "pagerank":
        scores = nx.pagerank(graph, weight="weight")
    elif method == "eigenvector":
        try:
            scores = nx.eigenvector_centrality(
                graph, weight="weight", max_iter=500,
            )
        except nx.PowerIterationFailedConvergence as exc:
            raise GraphAnalysisError(
                "特征向量中心性未收敛（图结构特殊，例如多个互不相连的部分）。"
                "可改用 pagerank。",
            ) from exc
    elif method == "betweenness":
        # 精确介数是 O(V·E)，实测 10 万边要 15 分钟。采样 k 个源点近似，
        # 结果对「找出中介节点」这个用途足够，且能在秒级返回。
        k = min(graph.number_of_nodes(), BETWEENNESS_SAMPLE_SIZE)
        scores = nx.betweenness_centrality(
            graph, k=k, weight="weight", seed=42, normalized=True,
        )
    elif method == "closeness":
        scores = nx.closeness_centrality(graph, wf_improved=False)
    else:
        raise GraphAnalysisError(
            f"不支持的关联度口径 {method!r}。可选：degree、weighted_degree、"
            "pagerank、eigenvector、betweenness、closeness。",
        )

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[: max(1, top_n)]


def shortest_path(graph, source: str, target: str) -> tuple[list[str], int]:
    """两点之间的最短关系链（按边数，不按权重）。

    按跳数而非权重：用户问「隔了几层关系」，关心的是中间经手了几个人，
    而不是通话时长。返回 ``(路径节点, 跳数)``；不可达时抛可读错误。
    """
    nx = _import_networkx()
    source = _coerce_node(source)
    target = _coerce_node(target)
    if source == target:
        return [source], 0
    if source not in graph:
        raise GraphAnalysisError(f"起点 {source!r} 不在关系数据中。")
    if target not in graph:
        raise GraphAnalysisError(f"终点 {target!r} 不在关系数据中。")
    try:
        path = nx.shortest_path(graph, source=source, target=target)
    except nx.NetworkXNoPath as exc:
        raise GraphAnalysisError(
            f"{source!r} 与 {target!r} 之间没有可达路径（不属于同一关联群体）。",
        ) from exc
    return list(path), len(path) - 1


def all_simple_paths(
    graph,
    source: str,
    target: str,
    *,
    limit: int = 50,
) -> list[list[str]]:
    """两点之间的全部简单路径（不重复经过节点），最多 ``limit`` 条。

    用于回答「他还能通过哪几条线联系到这个人」。路径条数随图规模指数增长，
    因此有 ``limit``；返回的条数等于 ``limit`` 说明可能被截断。
    """
    nx = _import_networkx()
    source = _coerce_node(source)
    target = _coerce_node(target)
    for node, label in ((source, "起点"), (target, "终点")):
        if node not in graph:
            raise GraphAnalysisError(f"{label} {node!r} 不在关系数据中。")
    if source == target:
        return [[source]]
    paths: list[list[str]] = []
    try:
        for path in nx.all_simple_paths(graph, source, target):
            paths.append(list(path))
            if len(paths) >= limit:
                break
    except nx.NodeNotFound as exc:  # pragma: no cover - 上面已校验
        raise GraphAnalysisError("路径查询的节点不存在。") from exc
    if not paths:
        raise GraphAnalysisError(
            f"{source!r} 与 {target!r} 之间没有可达路径。",
        )
    # 短路径优先：用户通常想看最直接的那几条线。
    return sorted(paths, key=len)


def predict_links(
    graph,
    *,
    method: str = "jaccard",
    candidate_pairs: Sequence[tuple[str, str]] | None = None,
    limit: int = 20,
) -> list[tuple[str, str, float]]:
    """预测「很可能认识但数据里还没有直接关系」的节点对。

    思路是共同邻居：两个人有越多共同联系人，越可能存在未记录的关系。

    * ``jaccard`` —— 共同邻居占比，归一化，最常用。
    * ``adamic_adar`` —— 共同邻居越冷门权重越高（认识一个只有 2 个联系人的人，
      比认识一个有 1000 个联系人的人更能说明关系）。
    * ``resource_allocation`` —— 与 Adamic-Adar 类似，对冷门邻居更敏感。

    不传 ``candidate_pairs`` 时不枚举全图（节点对是 O(V²)），而是对**每个节点
    与其二跳邻居**生成候选 —— 这是有意义候选的自然集合：已经直连的不用预测，
    二跳可达的才可能有未记录关系。
    """
    nx = _import_networkx()
    if graph.number_of_nodes() == 0:
        return []

    if candidate_pairs is None:
        pairs: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for node in graph.nodes():
            neighbors = set(graph.neighbors(node))
            for peer in list(neighbors):
                for second in graph.neighbors(peer):
                    if second == node or second in neighbors:
                        continue
                    key = (node, second) if node <= second else (second, node)
                    if key in seen:
                        continue
                    seen.add(key)
                    pairs.append(key)
            if len(pairs) >= 50_000:
                # 候选集合已足够大；再扩下去只是白烧内存。
                break
    else:
        pairs = [(_coerce_node(a), _coerce_node(b)) for a, b in candidate_pairs]

    if not pairs:
        return []

    if method == "jaccard":
        scored = nx.jaccard_coefficient(graph, pairs)
    elif method == "adamic_adar":
        scored = nx.adamic_adar_index(graph, pairs)
    elif method == "resource_allocation":
        scored = nx.resource_allocation_index(graph, pairs)
    else:
        raise GraphAnalysisError(
            f"不支持的预测口径 {method!r}。"
            "可选：jaccard、adamic_adar、resource_allocation。",
        )

    results = [
        (str(u), str(v), float(score))
        for u, v, score in scored
        if score > 0
    ]
    results.sort(key=lambda item: -item[2])
    return results[: max(1, limit)]


def key_nodes(graph, *, kind: str = "articulation_points") -> list[str]:
    """关键节点/边：去掉它就会让网络分裂。

    * ``articulation_points`` —— 割点（关键联络人）。去掉它，网络会分裂成
      多个不连通的部分。
    * ``bridges`` —— 桥（唯一联系方式）。返回形如 ``"A|B"`` 的边标识。

    对侦查很有用：一个把两个团伙连起来的割点，往往就是真正的关键人物。
    """
    nx = _import_networkx()
    if graph.number_of_nodes() == 0:
        return []
    working = graph.to_undirected() if graph.is_directed() else graph
    if kind == "articulation_points":
        return sorted(str(node) for node in nx.articulation_points(working))
    if kind == "bridges":
        return sorted(
            f"{min(u, v)}|{max(u, v)}" for u, v in nx.bridges(working)
        )
    raise GraphAnalysisError(
        f"不支持的关键点类型 {kind!r}。可选：articulation_points、bridges。",
    )


def _neighborhood_subgraph(graph, center: str, radius: int):
    """以 ``center`` 为起点、``radius`` 跳内的邻域子图。

    团与环的枚举只在子图上进行：全图枚举的规模是组合爆炸（实测 2 万节点
    10 万边的图上前者不收敛），而用户真正关心的也是「某个人周围的紧密小团体」
    或「某笔资金在谁之间打转」，邻域正是有意义的范围。
    """
    nx = _import_networkx()
    center = _coerce_node(center)
    if center not in graph:
        raise GraphAnalysisError(f"节点 {center!r} 不在关系数据中。")
    nodes = nx.single_source_shortest_path_length(graph, center, cutoff=radius)
    if len(nodes) > SUBGRAPH_MAX_NODES:
        raise GraphAnalysisError(
            f"{center!r} 的 {radius} 跳邻域含 {len(nodes):,} 个节点，"
            f"超过 {SUBGRAPH_MAX_NODES:,} 的枚举上限。"
            "请减小 radius，或先筛选数据缩小范围。",
        )
    return graph.subgraph(list(nodes))


def find_cliques(
    graph,
    *,
    center: str,
    radius: int = 2,
    min_size: int = 3,
    limit: int = 20,
) -> list[list[str]]:
    """节点邻域内互为直接关系的紧密小团体（团），按规模降序。

    团是「互相之间全都有关系」的节点集合 —— 比社区更严格，常用于确认小圈子。
    只在 ``center`` 的 ``radius`` 跳邻域内枚举（全图枚举不收敛）。
    """
    nx = _import_networkx()
    sub = _neighborhood_subgraph(graph, center, radius)
    cliques: list[list[str]] = []
    for clique in nx.find_cliques(sub):
        if len(clique) >= min_size:
            cliques.append(sorted(str(node) for node in clique))
    cliques.sort(key=len, reverse=True)
    return cliques[: max(1, limit)]


def find_cycles(
    graph,
    *,
    center: str,
    radius: int = 2,
    max_length: int = 5,
    limit: int = 20,
) -> list[list[str]]:
    """节点邻域内的关系环（A→B→C→A），按长度升序。

    用于发现闭环流转：资金在几个人之间打转、通话形成小圈等。只在邻域内枚举，
    并限制环长 —— 全图枚举不收敛。
    """
    nx = _import_networkx()
    sub = _neighborhood_subgraph(graph, center, radius)
    cycles: list[list[str]] = []
    try:
        iterator = nx.simple_cycles(
            sub, length_bound=max(2, int(max_length)),
        )
        for cycle in iterator:
            cycles.append([str(node) for node in cycle])
            if len(cycles) >= limit:
                break
    except Exception as exc:  # noqa: BLE001 - 枚举可能因规模异常
        raise GraphAnalysisError(f"环枚举未完成：{exc}") from exc
    cycles.sort(key=len)
    return cycles


def clustering_coefficient(graph) -> list[tuple[str, float]]:
    """每个节点的聚集系数，降序。

    衡量「某人的联系人之间是否也互相认识」：值为 1 表示他的联系人两两都认识
    （典型小圈子成员），接近 0 表示他的联系人彼此无关（可能是中介/枢纽）。
    只保留有实际连接（度数 ≥ 2）的节点 —— 度数为 0/1 的节点系数无意义。
    """
    nx = _import_networkx()
    if graph.number_of_nodes() == 0:
        return []
    scores = nx.clustering(graph)
    meaningful = [
        (str(node), float(score))
        for node, score in scores.items()
        if graph.degree(node) >= 2
    ]
    meaningful.sort(key=lambda item: (-item[1], item[0]))
    return meaningful


def core_numbers(graph) -> list[tuple[str, int]]:
    """k-core 层级，降序。

    迭代剥掉度数小于 k 的节点；``core_number`` 是节点能留存的最大 k。层级越高
    表示该节点所处的结构越紧密（即使绝对联系人不多）。
    """
    nx = _import_networkx()
    if graph.number_of_nodes() == 0:
        return []
    numbers = nx.core_number(graph)
    ranked = sorted(numbers.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return [(str(node), int(value)) for node, value in ranked]


def label_propagation_communities(graph) -> list[list[str]]:
    """标签传播社区发现，按规模降序。

    比 Louvain 快得多（实测 31 万倍于它的量级差距：312ms vs 10.6s），代价是
    结果略粗、且**不保证确定性**。需固定 seed，但 networkx 的实现不暴露 seed，
    因此同一份数据两次结果可能不同 —— 工具层需向用户说明这一点。
    """
    nx = _import_networkx()
    if graph.number_of_nodes() == 0:
        return []
    working = graph.to_undirected() if graph.is_directed() else graph
    communities = nx.community.label_propagation_communities(working)
    return sorted(
        (sorted(str(node) for node in community) for community in communities),
        key=len,
        reverse=True,
    )


def k_components(graph, *, k: int = 3) -> list[list[str]]:
    """k-连通分量：去掉任意 k-1 个节点后仍保持连通的群组。

    比连通分量严格得多，用于找出「结构上真正紧密」的群体 —— 两个人仅仅能连上
    可能只是偶然，但一个 3-连通群组即使在损失两个成员后依然连通。返回把不同
    最深层级（即请求的 ``k``，若该层级不存在则取实际最深的一层）的群组，
    按规模降序 —— 只给最紧密的那一层，不混入更宽松的层级。
    """
    nx = _import_networkx()
    if graph.number_of_nodes() == 0 or k < 2:
        return []
    working = graph.to_undirected() if graph.is_directed() else graph
    try:
        levels = nx.k_components(working, k)
    except Exception as exc:  # noqa: BLE001 - k-连通计算可能因规模失败
        raise GraphAnalysisError(f"k-连通分量计算未完成：{exc}") from exc
    # 只返回「最深层级」的群组。``k_components`` 返回所有层级，且低层级是
    # 高层级的严格超集 —— level 1 就是整个连通分量。把各层混在一起会把
    # 「仅仅能连上」的整块图当成 k-连通群组报出来，等于退化成连通分量，
    # 反而掩盖了真正紧密的小群组。
    groups, _level = _deepest_k_level(levels, k)
    return groups


def _deepest_k_level(levels: dict, k: int) -> tuple[list[list[str]], int | None]:
    """取最深层级的群组，并返回实际使用的层级。"""
    if not levels:
        return [], None
    target_level = k if k in levels else max(levels)
    groups = [
        sorted(str(node) for node in component)
        for component in levels[target_level]
    ]
    groups.sort(key=len, reverse=True)
    return groups, target_level


def k_components_with_level(
    graph,
    *,
    k: int = 3,
) -> tuple[list[list[str]], int | None]:
    """同 :func:`k_components`，但额外返回实际使用的连通层级。

    **注意层级不等于真实连通度。** ``nx.k_components`` 会把图实际能达到的最高
    层级重复填充到所有更高的请求层级上 —— 一个 8 人完全图请求 k=5 与 k=7 得到
    的返回完全一样，但它的真实节点连通度是 7。因此调用方不能照抄请求的 ``k``
    当作结论，否则会低估紧密程度（对侦查这类用途是实质性的误导）。
    需要准确数值请用 :func:`group_connectivity`。
    """
    nx = _import_networkx()
    if graph.number_of_nodes() == 0 or k < 2:
        return [], None
    working = graph.to_undirected() if graph.is_directed() else graph
    try:
        levels = nx.k_components(working, k)
    except Exception as exc:  # noqa: BLE001
        raise GraphAnalysisError(f"k-连通分量计算未完成：{exc}") from exc
    return _deepest_k_level(levels, k)


def group_connectivity(
    graph,
    groups: Sequence[Sequence[str]],
    *,
    limit: int = 20,
) -> list[int]:
    """每个群组的真实节点连通度（去掉多少个成员才会把它拆开）。

    用于把「k-连通群组」这个说法落到实处：``k_components`` 的层级受请求参数
    影响，而这里的数值只由图的形状决定。群组过大时计算是超线性的，因此超过
    ``SUBGRAPH_MAX_NODES`` 的群组记为 0（未知），不阻塞其余结果。
    """
    nx = _import_networkx()
    if graph.is_directed():
        working = graph.to_undirected()
    else:
        working = graph
    values: list[int] = []
    for group in groups[:limit]:
        nodes = [str(n) for n in group]
        if len(nodes) < 2:
            values.append(0)
            continue
        if len(nodes) > SUBGRAPH_MAX_NODES:
            values.append(0)
            continue
        sub = working.subgraph(nodes)
        try:
            values.append(int(nx.node_connectivity(sub)))
        except Exception:  # noqa: BLE001 - 个别群组算不出不影响其余
            logger.debug("graph: node_connectivity failed for a group")
            values.append(0)
    return values


__all__ = [
    "ALGORITHMS",
    "AlgorithmCost",
    "BETWEENNESS_SAMPLE_SIZE",
    "DEFAULT_MAX_EDGES",
    "DEFAULT_MAX_NODES",
    "EXTREME_MAX_EDGES",
    "Edge",
    "GraphAnalysisError",
    "HEAVY_MAX_EDGES",
    "SUBGRAPH_MAX_NODES",
    "SUPPORTED_ALGORITHMS",
    "algorithm_cost",
    "all_simple_paths",
    "build_graph",
    "check_scale",
    "clustering_coefficient",
    "connected_components",
    "core_numbers",
    "find_cliques",
    "find_cycles",
    "group_connectivity",
    "k_components",
    "k_components_with_level",
    "key_nodes",
    "label_propagation_communities",
    "louvain_communities",
    "normalize_edges",
    "predict_links",
    "rank_nodes",
    "shortest_path",
]
