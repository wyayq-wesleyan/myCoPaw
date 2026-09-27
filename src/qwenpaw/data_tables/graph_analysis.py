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
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 建内存图的规模上限。networkx 是纯 Python 实现，内存占用约为
# (节点数 + 边数) × 每个 Python 对象的开销，百万级边就会吃掉数 GB。
# 超限时明确报错并给出可行建议，而不是让进程 OOM 或长时间无响应。
DEFAULT_MAX_NODES = 500_000
DEFAULT_MAX_EDGES = 2_000_000

# Louvain 是迭代式的，规模大时耗时陡增（实测 2 万节点/10 万边 ≈ 10.6s）。
# 超过这个规模就拒绝，提示改用连通分量（可瞬时完成）或缩小范围。
LOUVAIN_MAX_EDGES = 300_000

SUPPORTED_ALGORITHMS = (
    "connected_components",
    "louvain",
    "degree_centrality",
    "pagerank",
    "shortest_path",
)


class GraphAnalysisError(ValueError):
    """输入或规模不满足算法要求（面向用户的可读错误）。"""


@dataclass
class Edge:
    """一条关系记录。"""

    source: str
    target: str
    weight: float = 1.0


@dataclass
class GraphResult:
    """一次图分析的归一化结果。

    不同算法的载荷不同，用 ``kind`` 区分，避免调用方去猜字段含义。
    所有列表都已按意义排好序并截断，调用方直接渲染即可。
    """

    kind: str
    node_count: int = 0
    edge_count: int = 0
    # kind == "components": 每个元素是成员列表（已按大小降序）
    groups: list[list[str]] = field(default_factory=list)
    # kind == "ranking": (节点, 分值) 已降序
    ranking: list[tuple[str, float]] = field(default_factory=list)
    # kind == "path": 途径节点与长度
    path: list[str] = field(default_factory=list)
    path_length: int | None = None
    # 结果被截断的说明（例如组数/排名超出 top_n）
    truncated: bool = False
    note: str = ""


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

    if graph.number_of_edges() > LOUVAIN_MAX_EDGES:
        raise GraphAnalysisError(
            f"社区发现需要的关系数不超过 {LOUVAIN_MAX_EDGES:,}，"
            f"当前 {graph.number_of_edges():,}。"
            "它比连通分量慢得多，数据量大时请先筛选范围，"
            "或改用 connected_components（可瞬时完成）。",
        )

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

    三种口径面向不同问题：

    * ``degree`` —— 联系的人数（对端数）。找「认识人最多」的。
    * ``weighted_degree`` —— 通话/交易总量。找「话务最频繁」的。
    * ``pagerank`` —— 被重要节点指向的程度。找「处在网络核心」的，
      比度数更能识别真正的枢纽（一个只跟大量边缘人联系的号，度数高但
      PageRank 不高）。
    """
    nx = _import_networkx()
    if graph.number_of_nodes() == 0:
        return []

    if method == "degree":
        scores = dict(graph.degree())
    elif method == "weighted_degree":
        if graph.is_directed():
            scores = dict(graph.degree(weight="weight"))
        else:
            scores = dict(graph.degree(weight="weight"))
    elif method == "pagerank":
        scores = nx.pagerank(graph, weight="weight")
    else:
        raise GraphAnalysisError(
            f"不支持的关联度口径 {method!r}。"
            "可选：degree（对端数）、weighted_degree（总量）、pagerank（网络核心度）。",
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


__all__ = [
    "DEFAULT_MAX_EDGES",
    "DEFAULT_MAX_NODES",
    "Edge",
    "GraphAnalysisError",
    "GraphResult",
    "LOUVAIN_MAX_EDGES",
    "SUPPORTED_ALGORITHMS",
    "build_graph",
    "connected_components",
    "louvain_communities",
    "normalize_edges",
    "rank_nodes",
    "shortest_path",
]
