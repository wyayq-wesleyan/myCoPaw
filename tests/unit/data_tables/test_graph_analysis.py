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
    """未知口径要报可读错误，而不是静默返回空。

    静默返回空会被误读成「数据里没有关系」，比报错更糟。
    """
    graph = ga.build_graph(ga.normalize_edges([("A", "B")]))
    with pytest.raises(ga.GraphAnalysisError, match="不支持的关联度口径"):
        ga.rank_nodes(graph, method="definitely_not_a_method")


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
    rows = [
        (f"n{i}", f"n{i + 1}")
        for i in range(ga.HEAVY_MAX_EDGES + 10)
    ]
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


# --- 扩充的算法（每个都对照已知答案）---------------------------------------


def _two_rings_bridged():
    """两个 4 人圈，由唯一一条边相连。

    这个形状对多个算法都有确定的答案，适合逐项校验：
    圈内两两互连（团），跨圈只有一条桥，两个圈交接处的节点是割点。
    """
    rows = []
    for group in ("X", "Y"):
        for a in range(1, 5):
            for b in range(a + 1, 5):
                rows.append((f"{group}{a}", f"{group}{b}"))
    rows.append(("X1", "Y1"))  # 唯一的桥
    return ga.build_graph(ga.normalize_edges(rows))


def test_clustering_coefficient_is_one_inside_a_clique():
    """团内每个节点的聚集系数都是 1（联系人之间两两认识）。

    这正是它识别小圈子成员的依据；若算法退化成全图平均值就失去意义。
    """
    graph = _two_rings_bridged()
    ranked = dict(ga.clustering_coefficient(graph))
    for node in ("X2", "X3", "X4"):
        assert ranked[node] == pytest.approx(1.0), node
    # X1 连到 Y1，那个联系人不在 X 圈内，所以系数小于 1。
    assert ranked["X1"] < 1.0


def test_clustering_excludes_nodes_without_enough_contacts():
    """度数为 0/1 的节点系数无意义，应被剔除。

    否则一个只有一条关系的节点会以系数 0 排进列表，看起来像「孤立者」，
    实际只是样本不足。
    """
    graph = ga.build_graph(ga.normalize_edges([("A", "B"), ("C", "D"), ("D", "E")]))
    ranked = dict(ga.clustering_coefficient(graph))
    assert "A" not in ranked  # 度数 1
    assert "C" not in ranked  # 度数 1


def test_core_numbers_identify_the_dense_core():
    """k-core 层级：团内节点层级应高于桥接节点。

    桥接节点虽然连接两个圈，但它所在的子结构没有圈内紧密。
    """
    graph = _two_rings_bridged()
    ranked = dict(ga.core_numbers(graph))
    assert ranked["X2"] >= ranked["X1"]


def test_articulation_points_find_the_bridge_endpoints():
    """割点必须是桥的两端 —— 去掉它们，两个圈就断开。

    这是「关键联络人」的判定依据，对侦查（谁把两个团伙连起来）直接有用。
    """
    graph = _two_rings_bridged()
    points = ga.key_nodes(graph, kind="articulation_points")
    assert points == ["X1", "Y1"]


def test_bridges_find_the_single_connecting_edge():
    """桥只有一条，且正是两个圈之间那条边。"""
    graph = _two_rings_bridged()
    assert ga.key_nodes(graph, kind="bridges") == ["X1|Y1"]


def test_key_nodes_has_no_false_positives_in_a_clique():
    """完全图里没有割点，也没有桥 —— 不存在「去掉就分裂」的点或边。

    这条防的是「把所有节点都报成关键点」这种看似有用实则误导的输出。
    """
    rows = [(f"N{a}", f"N{b}") for a in range(1, 6) for b in range(a + 1, 6)]
    graph = ga.build_graph(ga.normalize_edges(rows))
    assert ga.key_nodes(graph, kind="articulation_points") == []
    assert ga.key_nodes(graph, kind="bridges") == []


def test_all_simple_paths_enumerates_distinct_routes():
    """A 到 D 有三条不重复路径（经 B、经 C、经 B→C）。"""
    graph = ga.build_graph(
        ga.normalize_edges(
            [("A", "B"), ("B", "D"), ("A", "C"), ("C", "D"), ("B", "C")],
        ),
    )
    paths = ga.all_simple_paths(graph, "A", "D")
    as_sets = {tuple(p) for p in paths}
    assert ("A", "B", "D") in as_sets
    assert ("A", "C", "D") in as_sets
    assert ("A", "B", "C", "D") in as_sets
    # 短路径优先。
    assert paths[0] == ["A", "B", "D"] or paths[0] == ["A", "C", "D"]


def test_all_simple_paths_respects_the_limit():
    """路径数是指数级的，必须有上限 —— 否则大图上会耗尽内存。"""
    rows = [(f"n{i}", f"n{i + 1}") for i in range(6)]
    rows += [(f"n{i}", f"n{i + 2}") for i in range(4)]
    graph = ga.build_graph(ga.normalize_edges(rows))
    paths = ga.all_simple_paths(graph, "n0", "n5", limit=2)
    assert len(paths) <= 2


def test_all_simple_paths_unreachable_is_readable():
    graph = ga.build_graph(ga.normalize_edges([("A", "B"), ("C", "D")]))
    with pytest.raises(ga.GraphAnalysisError, match="没有可达路径"):
        ga.all_simple_paths(graph, "A", "C")


def test_label_propagation_recovers_the_two_rings():
    """标签传播应把两个圈分开（与 Louvain 的结论一致）。"""
    graph = _two_rings_bridged()
    communities = [set(c) for c in ga.label_propagation_communities(graph)]
    assert {frozenset({f"X{i}" for i in range(1, 5)}), frozenset({f"Y{i}" for i in range(1, 5)})} == {
        frozenset(c) for c in communities
    }


def test_k_components_is_stricter_than_connected_components():
    """k-连通比连通分量严格：两个圈由一条桥相连时，它把两个圈分开而不是并成一群。

    这正是它的价值 —— 「仅仅能连上」不等于「结构紧密」。连通分量只能给出
    「1 个 8 人群体」，k-连通给出「2 个 4 人圈」。
    """
    graph = _two_rings_bridged()
    assert ga.connected_components(graph) == [
        ["X1", "X2", "X3", "X4", "Y1", "Y2", "Y3", "Y4"],
    ]
    assert ga.k_components(graph, k=3) == [
        ["X1", "X2", "X3", "X4"],
        ["Y1", "Y2", "Y3", "Y4"],
    ]


def test_k_components_reports_only_the_deepest_level():
    """只返回最深层级，不把更宽松的层级混进来。

    ``k_components`` 会返回 level 1..k，而低层级是高层的严格超集（level 1
    就是整个连通分量）。混在一起会把整块图当成 k-连通群组报出来，退化成
    连通分量，反而盖住真正紧密的小群组。
    """
    graph = _two_rings_bridged()
    groups = ga.k_components(graph, k=3)
    assert all(len(group) == 4 for group in groups), "不该出现覆盖全图的群组"


def test_predict_links_ranks_common_neighbours_highest():
    """共同联系人越多，分值越高。"""
    # A-B-C 链 + D 同时连 A 与 C：D 与 B 有一个很强的共同邻居关系。
    graph = ga.build_graph(
        ga.normalize_edges(
            [("A", "B"), ("B", "C"), ("D", "A"), ("D", "C"), ("D", "B")],
        ),
    )
    # D 与 A/C 已直连，因此候选里不该再出现它们。
    results = ga.predict_links(graph, method="jaccard", limit=10)
    pairs = {(u, v) for u, v, _s in results}
    assert ("A", "C") in pairs, "A 与 C 有共同联系人 B、D，应被预测"
    assert ("A", "B") not in pairs, "已直连的不该出现在预测里"


def test_predict_links_rejects_unknown_method():
    graph = ga.build_graph(ga.normalize_edges([("A", "B"), ("B", "C")]))
    with pytest.raises(ga.GraphAnalysisError, match="不支持的预测口径"):
        ga.predict_links(graph, method="nope")


def test_find_cliques_returns_the_full_clique():
    """团必须是互相两两直连的完整集合。"""
    graph = _two_rings_bridged()
    cliques = ga.find_cliques(graph, center="X2", radius=2, min_size=4)
    biggest = max(cliques, key=len)
    assert len(biggest) == 4
    assert biggest == ["X1", "X2", "X3", "X4"]


def test_find_cliques_respects_min_size():
    """小于 min_size 的团不返回，避免噪声淹没结论。"""
    graph = ga.build_graph(ga.normalize_edges([("A", "B"), ("B", "C")]))
    # 只有一条链，没有任何 3 团。
    assert ga.find_cliques(graph, center="B", radius=2, min_size=3) == []


def test_find_cycles_finds_a_triangle():
    """三点闭环应被找到，且闭合展示。"""
    graph = ga.build_graph(
        ga.normalize_edges([("A", "B"), ("B", "C"), ("C", "A")]),
    )
    cycles = ga.find_cycles(graph, center="A", radius=2, max_length=3)
    assert [sorted(c) for c in cycles] == [["A", "B", "C"]]


def test_cliques_and_cycles_require_a_real_center():
    """中心节点不存在时要给可读错误，而不是空结果。

    空结果会被误读成「这个人身边没有紧密团体」。
    """
    graph = ga.build_graph(ga.normalize_edges([("A", "B")]))
    with pytest.raises(ga.GraphAnalysisError, match="不在关系数据中"):
        ga.find_cliques(graph, center="ZZZ")
    with pytest.raises(ga.GraphAnalysisError, match="不在关系数据中"):
        ga.find_cycles(graph, center="ZZZ")


def test_eigenvector_centrality_ranks_the_hub_first():
    graph = ga.build_graph(
        ga.normalize_edges([("HUB", "L1"), ("HUB", "L2"), ("HUB", "L3")]),
    )
    ranked = dict(ga.rank_nodes(graph, method="eigenvector", top_n=10))
    assert ranked["HUB"] == max(ranked.values())


def test_betweenness_is_sampled_and_marks_the_bridge():
    """介数中心性必须采样（精确版在十万级关系上要十几分钟）。

    这里验证采样后仍能识别桥接节点 —— 这正是介数的用途。
    """
    graph = _two_rings_bridged()
    ranked = dict(ga.rank_nodes(graph, method="betweenness", top_n=10))
    assert ranked["X1"] > ranked["X2"], "桥接点的中介作用应高于圈内成员"


# --- 成本分级 ---------------------------------------------------------------


def test_every_algorithm_declares_a_cost_tier():
    """每个算法都必须声明成本档位。

    没有档位就意味着没有规模上限：介数中心性精确版在十万级关系上要 15 分钟，
    漏掉分级会让用户点一下就把服务挂住。
    """
    for name, cost in ga.ALGORITHMS.items():
        assert cost.tier in ("light", "heavy", "extreme"), name
        assert cost.description, name
        if cost.tier != "light":
            assert cost.max_edges is not None, f"{name} 是重算法却没有上限"


def test_light_algorithms_have_no_scale_cap():
    """轻量算法必须能在任意规模上跑通 —— 给它们加上限会挡住正常用法。"""
    big = ga.build_graph(
        ga.normalize_edges([(f"n{i}", f"n{i + 1}") for i in range(1000)]),
    )
    for name in ("connected_components", "pagerank", "degree_centrality", "core_number"):
        ga.check_scale(name, big)  # 不抛异常


def test_extreme_algorithms_are_capped_much_lower_than_heavy():
    """极重算法的上限必须显著低于重算法，否则分级失去意义。"""
    assert (
        ga.ALGORITHMS["betweenness_centrality"].max_edges
        < ga.ALGORITHMS["louvain"].max_edges
    )
    assert (
        ga.ALGORITHMS["closeness_centrality"].max_edges
        < ga.ALGORITHMS["label_propagation"].max_edges
    )


def test_scale_check_message_names_a_faster_alternative():
    """超限信息必须给出可执行的替代方案，而不是只说「太大了」。"""
    graph = ga.build_graph(
        ga.normalize_edges(
            [(f"n{i}", f"n{i + 1}") for i in range(ga.EXTREME_MAX_EDGES + 10)],
        ),
    )
    with pytest.raises(ga.GraphAnalysisError) as excinfo:
        ga.check_scale("betweenness_centrality", graph)
    message = str(excinfo.value)
    assert "pagerank" in message
    assert "where" in message


def test_unknown_algorithm_is_rejected_with_the_full_list():
    graph = ga.build_graph(ga.normalize_edges([("A", "B")]))
    with pytest.raises(ga.GraphAnalysisError) as excinfo:
        ga.check_scale("does_not_exist", graph)
    assert "connected_components" in str(excinfo.value)


def test_subgraph_enumeration_caps_the_neighbourhood():
    """团/环枚举必须限制邻域规模 —— 全图枚举不收敛。

    造一个明显超过 SUBGRAPH_MAX_NODES 的图，radius 足够大以覆盖全图。
    """
    size = ga.SUBGRAPH_MAX_NODES + 100
    graph = ga.build_graph(
        ga.normalize_edges([(f"n{i}", f"n{i + 1}") for i in range(size)]),
    )
    with pytest.raises(ga.GraphAnalysisError, match="枚举上限"):
        ga.find_cliques(graph, center="n0", radius=size)


def test_group_connectivity_reports_the_true_value_not_the_requested_k():
    """连通度必须由图的实际形状决定，不能照抄请求的 k。

    ``nx.k_components`` 会把图能达到的最高层级**重复填充**到所有更高的请求
    层级上：8 人完全图请求 k=5 与 k=7 返回一样，但真实连通度是 7。照抄 k 会
    低估紧密程度 —— 对「这个团伙有多难拆散」这种判断是实质性误导。
    """
    clique = ga.build_graph(
        ga.normalize_edges(
            [(f"N{a}", f"N{b}") for a in range(1, 7) for b in range(a + 1, 7)],
        ),
    )
    groups = [sorted(clique.nodes())]
    # 6 人完全图：去掉 5 个成员才会拆散。
    assert ga.group_connectivity(clique, groups) == [5]


def test_group_connectivity_of_a_chain_is_one():
    """链式结构的连通度是 1（去掉任意一个中间人就会断）。"""
    graph = ga.build_graph(
        ga.normalize_edges([("A", "B"), ("B", "C"), ("C", "D")]),
    )
    assert ga.group_connectivity(graph, [["A", "B", "C", "D"]]) == [1]


def test_group_connectivity_handles_oversized_groups_without_failing():
    """过大的群组记为 0（未知），不阻塞其余结果。

    节点连通度是超线性的；一个大群组算不出来时不该让整次分析失败。
    """
    size = ga.SUBGRAPH_MAX_NODES + 10
    graph = ga.build_graph(
        ga.normalize_edges([(f"n{i}", f"n{i + 1}") for i in range(size)]),
    )
    big_group = [f"n{i}" for i in range(size + 1)]
    assert ga.group_connectivity(graph, [big_group]) == [0]


def test_every_declared_algorithm_is_actually_implemented():
    """清单里的每个算法都要能在工具层被派发。

    清单与实现分处两个模块，容易只加清单不加实现 —— 那样模型会调到一个
    声称存在但实际不工作的算法。
    """
    import inspect

    from qwenpaw.agents.tools import graph_analysis as tool

    dispatch = inspect.getsource(tool._run_analysis)
    for name in ga.ALGORITHMS:
        assert f'"{name}"' in dispatch, f"{name} 在清单里但没有派发实现"
