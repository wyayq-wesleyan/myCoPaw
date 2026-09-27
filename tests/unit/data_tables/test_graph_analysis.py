# -*- coding: utf-8 -*-
"""图分析算法的单元测试。

用**已知答案**的小图验证每个算法，而不是只断言「跑通了」—— 图算法最危险的
失败模式是"给出一个看起来合理但错误的结果"：我自己在实现过程中就写过一个
标签传播版本，它 86ms 返回且不报错，但把 250 人的团伙算成了 5 人。所以这里
每个算法都对照手工构造的预期结果。
"""

from __future__ import annotations

import pytest

from qwenpaw.data_tables import graph_analysis as ga


# --- 连通分量 ---------------------------------------------------------------


def test_connected_components_matches_known_partition():
    """两个圈子 + 自环：自环应被丢弃，分量按规模降序。"""
    edges = ga.normalize_edges(
        [
            ("A1", "A2"), ("A2", "A3"), ("A3", "A4"), ("A4", "A1"),
            ("B1", "B2"), ("B2", "B3"),
            ("C1", "C1"),  # 自环：自己打给自己，不构成关系
        ],
    )
    assert len(edges) == 6  # 7 行 - 1 个自环
    components = ga.connected_components(ga.build_graph(edges))
    assert components == [["A1", "A2", "A3", "A4"], ["B1", "B2", "B3"]]


def test_isolated_nodes_are_excluded_from_multi_person_groups():
    """只有单点的分量不该被当成「团伙」。"""
    edges = ga.normalize_edges([("A", "B"), ("C", "C")])
    components = ga.connected_components(ga.build_graph(edges))
    assert components == [["A", "B"]]


def test_components_handle_no_edges():
    """空图不能抛错。"""
    assert ga.connected_components(ga.build_graph([])) == []


# --- 关联度 -----------------------------------------------------------------


def test_degree_and_weighted_degree_measure_different_things():
    """度数看「认识多少人」，加权看「联系多频繁」—— 两者必须能给出不同排名。

    这正是要提供多个口径的原因：一个只跟三个边缘人高频联系的号，度数不高但
    话务量最大；反过来，一个跟很多人偶尔联系的号，度数高但量不大。
    """
    edges = ga.normalize_edges(
        [
            ("HUB", "L1", 100), ("HUB", "L2", 100), ("HUB", "L3", 100),
            ("C1", "C2", 10), ("C2", "C3", 10), ("C3", "C1", 10),
            ("C1", "C4", 10),
        ],
    )
    graph = ga.build_graph(edges)

    by_degree = dict(ga.rank_nodes(graph, method="degree", top_n=10))
    by_weight = dict(ga.rank_nodes(graph, method="weighted_degree", top_n=10))

    # HUB 与 C1 的对端数都是 3（度数并列）……
    assert by_degree["HUB"] == 3
    assert by_degree["C1"] == 3
    # ……但加权后 HUB 明显更大。
    assert by_weight["HUB"] == 300.0
    assert by_weight["HUB"] > by_weight["C1"]

    # 排序必须降序。
    scores = [score for _node, score in ga.rank_nodes(graph, method="degree")]
    assert scores == sorted(scores, reverse=True)


def test_pagerank_prefers_the_hub_over_peripheral_leaf():
    """PageRank 应把网络核心排在边缘叶节点之前。"""
    edges = ga.normalize_edges(
        [("HUB", "L1"), ("HUB", "L2"), ("HUB", "L3"), ("HUB", "L4")],
    )
    ranked = dict(ga.rank_nodes(ga.build_graph(edges), method="pagerank"))
    assert ranked["HUB"] > ranked["L1"]


def test_unknown_ranking_method_is_rejected():
    """未知口径要报可读错误，而不是静默返回空。"""
    graph = ga.build_graph(ga.normalize_edges([("A", "B")]))
    with pytest.raises(ga.GraphAnalysisError, match="不支持的关联度口径"):
        ga.rank_nodes(graph, method="betweenness")


# --- 社区发现 ---------------------------------------------------------------


def test_louvain_splits_two_dense_circles_joined_by_a_weak_bridge():
    """这是 Louvain 相对连通分量唯一不可替代的价值。

    两个各自内部密集、之间只有一条弱连接的圈子：连通分量只能给出「1 个 12 人
    的群体」，而 Louvain 能切成 2 个真实的圈子。如果这条断言退化成 1 个，说明
    权重没被传入，算法就退化成连通分量了。
    """
    rows = []
    for group in ("X", "Y"):
        for a in range(1, 7):
            for b in range(a + 1, 7):
                rows.append((f"{group}{a}", f"{group}{b}", 10))
    rows.append(("X1", "Y1", 1))  # 唯一弱桥

    graph = ga.build_graph(ga.normalize_edges(rows))

    components = ga.connected_components(graph)
    assert len(components) == 1  # 确实连通
    assert len(components[0]) == 12

    communities = [sorted(c) for c in ga.louvain_communities(graph)]
    assert len(communities) == 2, "Louvain 应把两个圈子分开"
    assert {frozenset(c) for c in communities} == {
        frozenset(f"X{i}" for i in range(1, 7)),
        frozenset(f"Y{i}" for i in range(1, 7)),
    }


def test_louvain_is_deterministic():
    """同一份数据必须每次得到同样的划分。

    Louvain 内部有随机性；结果不稳定的话用户无法复核，也无法用于自动化。
    """
    rows = [(f"n{i}", f"n{(i * 7) % 60}") for i in range(200)]
    graph = ga.build_graph(ga.normalize_edges(rows))
    first = ga.louvain_communities(graph)
    second = ga.louvain_communities(graph)
    assert first == second


def test_louvain_refuses_oversized_input_with_actionable_message():
    """超限要明确拒绝并给出替代方案，而不是跑几分钟后 OOM。"""
    rows = [(f"n{i}", f"n{i + 1}") for i in range(ga.LOUVAIN_MAX_EDGES + 10)]
    graph = ga.build_graph(ga.normalize_edges(rows))
    with pytest.raises(ga.GraphAnalysisError, match="connected_components"):
        ga.louvain_communities(graph)


# --- 关系链 -----------------------------------------------------------------


def test_shortest_path_reports_hops_and_route():
    """跳数与途经节点都要给出 —— 用户问的是「经过谁」。"""
    graph = ga.build_graph(
        ga.normalize_edges([("A", "B"), ("B", "C"), ("C", "D")]),
    )
    path, hops = ga.shortest_path(graph, "A", "D")
    assert hops == 3
    assert path == ["A", "B", "C", "D"]


def test_shortest_path_by_hops_not_weight():
    """按跳数而非权重。

    用户关心的是「中间隔了几个人」，不是通话时长；若按权重，一个高频单跳
    可能被算得比三跳更"远"。
    """
    graph = ga.build_graph(
        ga.normalize_edges([("A", "B", 0.001), ("B", "C", 0.001), ("C", "D", 0.001)]),
    )
    _path, hops = ga.shortest_path(graph, "A", "D")
    assert hops == 3


def test_shortest_path_same_node_is_zero_hops():
    graph = ga.build_graph(ga.normalize_edges([("A", "B")]))
    assert ga.shortest_path(graph, "A", "A") == (["A"], 0)


def test_shortest_path_unreachable_is_a_readable_error():
    """互不连通时给出可读解释，而不是抛 networkx 的异常类型。"""
    graph = ga.build_graph(ga.normalize_edges([("A", "B"), ("C", "D")]))
    with pytest.raises(ga.GraphAnalysisError, match="没有可达路径"):
        ga.shortest_path(graph, "A", "C")


def test_shortest_path_unknown_node_is_a_readable_error():
    graph = ga.build_graph(ga.normalize_edges([("A", "B")]))
    with pytest.raises(ga.GraphAnalysisError, match="不在关系数据中"):
        ga.shortest_path(graph, "A", "ZZZ")


# --- 输入归一化 -------------------------------------------------------------


def test_numeric_and_text_ids_land_on_the_same_node():
    """``13800138000`` 与 ``"13800138000"`` 必须是同一个节点。

    号码在库里可能是 bigint 或 text；不归一的话同一号码会被拆成两个点，
    团伙规模会被算小、关联度会被算低。
    """
    edges = ga.normalize_edges([(13800138000, "13800138001"), ("13800138001", "13800138002")])
    graph = ga.build_graph(edges)
    assert graph.number_of_nodes() == 3
    assert "13800138000" in graph


def test_float_integral_ids_do_not_gain_a_decimal_point():
    """避免 ``13800138000.0`` 这种脏节点名。"""
    edges = ga.normalize_edges([(13800138000.0, "A")])
    assert edges[0].source == "13800138000"


def test_repeated_pairs_accumulate_weight():
    """同一对关系出现多次要累加，而不是覆盖。

    话单里同两人多次通话必然是多条记录；覆盖会让「通话次数」只剩最后一条。
    """
    edges = ga.normalize_edges([("A", "B", 3), ("A", "B", 4)])
    graph = ga.build_graph(edges)
    assert graph["A"]["B"]["weight"] == 7.0
    assert graph.number_of_edges() == 1


def test_negative_and_zero_weights_are_made_safe():
    """负权与 0 权会让 PageRank / 最短路失去意义，归一为 0 并保留边。"""
    edges = ga.normalize_edges([("P", "Q", -5), ("Q", "R", 0)])
    assert all(edge.weight >= 0 for edge in edges)
    # 边仍然存在（关系存在，只是强度为 0）。
    assert ga.build_graph(edges).number_of_edges() == 2


def test_self_loops_are_dropped():
    edges = ga.normalize_edges([("A", "A"), ("A", "B")])
    assert [(e.source, e.target) for e in edges] == [("A", "B")]


def test_missing_node_identifier_is_rejected():
    with pytest.raises(ga.GraphAnalysisError, match="空的节点标识"):
        ga.normalize_edges([("A", None)])


def test_row_with_one_column_is_rejected():
    with pytest.raises(ga.GraphAnalysisError, match="至少需要"):
        ga.normalize_edges([("A",)])


def test_unparsable_weight_is_rejected():
    with pytest.raises(ga.GraphAnalysisError, match="权重无法解析"):
        ga.normalize_edges([("A", "B", "abc")])


# --- 规模上限 ---------------------------------------------------------------


def test_node_cap_is_enforced_before_allocating_the_graph():
    """超限要在建图前就拒绝 —— 建完再检查等于已经吃过内存了。"""
    edges = ga.normalize_edges([(f"n{i}", f"n{i + 1}") for i in range(10)])
    with pytest.raises(ga.GraphAnalysisError, match="节点数"):
        ga.build_graph(edges, max_nodes=5)


def test_edge_cap_is_enforced():
    edges = ga.normalize_edges([(f"n{i}", f"n{i + 1}") for i in range(10)])
    with pytest.raises(ga.GraphAnalysisError, match="关系数"):
        ga.build_graph(edges, max_edges=5)


def test_caps_allow_normal_sized_analysis():
    """上限不能低到挡住正常用法。"""
    edges = ga.normalize_edges([(f"n{i}", f"n{i + 1}") for i in range(1000)])
    graph = ga.build_graph(edges)
    assert graph.number_of_nodes() == 1001


# --- 有向图语义 -------------------------------------------------------------


def test_directed_graph_uses_weak_connectivity_for_components():
    """有向输入按弱连通处理（忽略方向看能否到达）。

    「关联群体」问的是有没有关系，不是方向；若按强连通，A→B 会被拆成两个
    单人分量，团伙分析直接失去意义。
    """
    edges = ga.normalize_edges([("A", "B"), ("B", "C")])
    graph = ga.build_graph(edges, directed=True)
    components = ga.connected_components(graph)
    assert components == [["A", "B", "C"]]


# --- 工具层：标识符校验与隔离 ----------------------------------------------


def test_identifier_validation_blocks_sql_injection():
    """表名/列名无法参数化，只能白名单校验。

    黑名单（只挡引号分号）会漏掉空格、换行等能改变语句结构的字符 —— 例如
    ``a b`` 拼接后会变成两个标识符；``a\\nb`` 有可能被驱动按行拆解。这里用
    允许列表，任何非「字母/数字/下划线/中文」一律拒绝。
    """
    from qwenpaw.agents.tools.graph_analysis import _validate_identifier

    dangerous = [
        't"; DROP TABLE x; --',
        "a b",
        "a\nb",
        "x' OR '1'='1",
        "a`b",
        "a--b",
        "a;b",
        "a.b",
        "a-b",
        "a/b",
        "a(b)",
    ]
    for value in dangerous:
        with pytest.raises(ga.GraphAnalysisError):
            _validate_identifier(value, label="列名")


def test_identifier_validation_accepts_real_world_names():
    """合法名字必须通过，包括中文列头（导入的 CSV 常是中文）。"""
    from qwenpaw.agents.tools.graph_analysis import _validate_identifier

    for value in ("tb_data123", "主叫号码", "被叫号码", "通话次数", "user_2", "_x"):
        assert _validate_identifier(value, label="列名") == value


def test_identifier_validation_rejects_empty_and_oversized():
    from qwenpaw.agents.tools.graph_analysis import _validate_identifier

    with pytest.raises(ga.GraphAnalysisError, match="不能为空"):
        _validate_identifier("", label="表名")
    with pytest.raises(ga.GraphAnalysisError, match="过长"):
        _validate_identifier("x" * 64, label="表名")


def test_read_edges_scopes_to_the_calling_account():
    """读边必须走「当前账号自己的库」。

    这条静态守卫确保没有绕过 per-account 数据库去连共享库 —— 隔离是
    storage 层保证的（每个账号一个 PG 数据库），一旦这里改成公共连接，
    隔离就只剩应用层过滤了。
    """
    import inspect

    from qwenpaw.agents.tools import graph_analysis as tool

    source = inspect.getsource(tool._read_edges)
    assert "ensure_user_database(username)" in source
    assert "_resolve_username()" in source
    # 不允许出现直连共享库的写法。
    assert "get_conn()" not in source.replace("get_target_connection", "")


def test_tool_is_registered_as_an_internal_readonly_tool():
    """注册契约：internal 类型、默认放行（只读且限本账号库）。"""
    from qwenpaw.agents.tools import graph_analysis as tool

    desc = tool.analyze_graph._tool_descriptor
    assert desc.name == "analyze_graph"
    assert desc.governance.tool_type == "internal"
    assert desc.governance.policy_name == "AnalyzeGraph"
    assert desc.async_execution is True


def test_tool_requires_the_edge_endpoints():
    """source/target 列是必需的 —— 没有它们无法构成关系。

    缺省成可选会让模型漏传列名，然后拿到一个「0 个节点」的空结果，误以为
    数据里没有关系。
    """
    import inspect

    from qwenpaw.agents.tools import graph_analysis as tool

    params = inspect.signature(tool.analyze_graph).parameters
    for name in ("table_name", "source_column", "target_column", "algorithm"):
        assert params[name].default is inspect.Parameter.empty, name
    # 可选参数应有默认值，模型可以不传。
    for name in ("weight_column", "source", "target", "where"):
        assert params[name].default is None, name
