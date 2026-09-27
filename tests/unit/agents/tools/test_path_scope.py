# -*- coding: utf-8 -*-
"""路径强制的统一入口（path_scope）测试。

这个模块是「这个智能体能看什么」的唯一答案来源。三个强制点（知识工具、数据表
工具、控制台列表）都必须走它 —— 各自重写规则是隔离静默失效的典型原因：漏掉
「未绑定=根路径」会隐藏公共材料，漏掉授权检查会暴露案件。

因此这里重点验证两件事：
1. 判定与 paths 模块的模型一致（公共区 / 隔离区）；
2. **任何不确定都判为拒绝**（fail-closed），而不是放行。
"""

from __future__ import annotations

import uuid

import pytest

from qwenpaw.platform_store import get_conn, put_conn
from qwenpaw.platform_store import paths as P

from ...platform_store.test_legacy_import import (  # noqa: E402
    requires_platform_db,
)


@pytest.fixture()
def scope(monkeypatch):
    """提供一个账号 + 可切换智能体身份的辅助对象。"""
    from qwenpaw.agents.tools import path_scope

    owner = f"scope-{uuid.uuid4().hex[:10]}"
    state = {"agent": ""}

    def set_agent(agent_id: str) -> None:
        state["agent"] = agent_id
        monkeypatch.setattr(
            path_scope, "current_agent_id", lambda: state["agent"],
        )

    yield owner, set_agent, path_scope

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                'DELETE FROM "platform".paths WHERE owner_username = %s',
                (owner,),
            )
            cur.execute(
                'DELETE FROM "platform".agent_path_bindings WHERE agent_id LIKE %s',
                (f"{owner}%",),
            )
            cur.execute(
                'DELETE FROM "platform".resource_bindings WHERE resource_id LIKE %s',
                (f"{owner}%",),
            )
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
    finally:
        put_conn(conn)


@requires_platform_db
def test_agent_with_root_grant_sees_public_material(scope):
    """绑了根路径的智能体能看到未绑定（公共）材料。

    这是升级后的基准行为：所有既有材料都未绑定，所有既有智能体都只绑了根，
    因此可见范围与升级前完全相同。
    """
    owner, set_agent, ps = scope
    root = P.ensure_root_path(owner, "公共")
    public = f"{owner}-公共资料"
    P.bind_agent(f"{owner}-ag", root)
    set_agent(f"{owner}-ag")

    assert ps.visible_space_ids(owner, [public]) == [public]


@requires_platform_db
def test_case_material_is_hidden_from_root_only_agent(scope):
    """绑到案件路径的材料，对只绑根的智能体不可见。

    隔离的操作方式就是这一步：把材料绑进案件路径。
    """
    owner, set_agent, ps = scope
    root = P.ensure_root_path(owner, "公共")
    case = P.create_path(owner, "案件一")
    case_material = f"{owner}-案件材料"
    P.bind_resource(case, P.KIND_KNOWLEDGE_SPACE, case_material)
    P.bind_agent(f"{owner}-ag", root)
    set_agent(f"{owner}-ag")

    assert ps.visible_space_ids(owner, [case_material]) == []


@requires_platform_db
def test_case_agent_sees_its_case_and_loses_it_when_material_moves(scope):
    """案件智能体可见本案材料；材料移走后立即不可见。"""
    owner, set_agent, ps = scope
    root = P.ensure_root_path(owner, "公共")
    case1 = P.create_path(owner, "案件一")
    case2 = P.create_path(owner, "案件二")
    material = f"{owner}-材料"

    P.bind_resource(case1, P.KIND_KNOWLEDGE_SPACE, material)
    for pid in (root, case1):
        P.bind_agent(f"{owner}-ag", pid)
    set_agent(f"{owner}-ag")
    assert ps.visible_space_ids(owner, [material]) == [material]

    P.bind_resource(case2, P.KIND_KNOWLEDGE_SPACE, material)
    assert ps.visible_space_ids(owner, [material]) == []


@requires_platform_db
def test_agent_without_grants_sees_nothing(scope):
    """完全没有授权的智能体什么都看不到（fail-closed）。"""
    owner, set_agent, ps = scope
    P.ensure_root_path(owner, "公共")
    set_agent(f"{owner}-ghost")

    assert ps.visible_space_ids(owner, [f"{owner}-公共资料"]) == []


@requires_platform_db
def test_unknown_agent_identity_denies(scope):
    """身份解析不出来时判为拒绝。

    与 ``resolve_account_username`` 同向：把「身份未知」当成「就是管理员」是
    越权，必须避免。
    """
    owner, set_agent, ps = scope
    root = P.ensure_root_path(owner, "公共")
    P.bind_agent(f"{owner}-ag", root)  # 即使账号下有授权
    set_agent("")  # 但这次调用拿不到身份

    assert ps.visible_space_ids(owner, [f"{owner}-资料"]) == []


@requires_platform_db
def test_scope_filters_only_the_agents_own_spaces(scope):
    """多个空间一起传进来时，只放行该智能体被授权的那些。"""
    owner, set_agent, ps = scope
    root = P.ensure_root_path(owner, "公共")
    case = P.create_path(owner, "案件一")
    public = f"{owner}-公共"
    case_material = f"{owner}-案件"
    P.bind_resource(case, P.KIND_KNOWLEDGE_SPACE, case_material)
    for pid in (root, case):
        P.bind_agent(f"{owner}-ag", pid)
    set_agent(f"{owner}-ag")

    visible = ps.visible_space_ids(owner, [public, case_material])
    assert set(visible) == {public, case_material}

    # 换成只绑根的智能体：案件材料被滤掉。
    P.bind_agent(f"{owner}-ag2", root)
    set_agent(f"{owner}-ag2")
    assert ps.visible_space_ids(owner, [public, case_material]) == [public]


@requires_platform_db
def test_empty_input_stays_empty(scope):
    owner, set_agent, ps = scope
    set_agent(f"{owner}-ag")
    assert ps.visible_space_ids(owner, []) == []


@requires_platform_db
def test_store_failure_denies_instead_of_allowing(scope, monkeypatch):
    """路径存储不可用时判为拒绝。

    放行意味着一次数据库故障就同时失去所有隔离边界 —— 这是最不该 fail-open
    的地方。
    """
    owner, set_agent, ps = scope
    set_agent(f"{owner}-ag")

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated store outage")

    monkeypatch.setattr(P, "agent_path_ids", boom)
    assert ps.visible_space_ids(owner, [f"{owner}-资料"]) == []


def test_current_agent_id_rejects_the_default_placeholder(monkeypatch):
    """``"default"`` 占位符不算真实智能体身份。

    部署里恰好有一个真名为 ``default`` 的智能体、owner 是管理员；把占位符当
    身份会让「身份未知」变成「以管理员身份行事」。
    """
    from qwenpaw.app import agent_context
    from qwenpaw.agents.tools import path_scope

    monkeypatch.setattr(
        agent_context, "get_current_agent_id", lambda: "default",
    )
    assert path_scope.current_agent_id() == ""

    monkeypatch.setattr(
        agent_context, "get_current_agent_id", lambda: "real-agent",
    )
    assert path_scope.current_agent_id() == "real-agent"


def test_current_agent_id_never_raises(monkeypatch):
    """身份解析异常不能让工具崩溃 —— 返回空串（随后判为拒绝）。"""
    from qwenpaw.app import agent_context
    from qwenpaw.agents.tools import path_scope

    def boom():
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(agent_context, "get_current_agent_id", boom)
    assert path_scope.current_agent_id() == ""


# --- 数据表 search_path 解析 ------------------------------------------------


@requires_platform_db
def test_data_search_path_is_public_only_for_a_root_grant(scope):
    """只授予根路径时，search_path 就是 ``['public']``。

    根路径映射到 ``public`` schema，这正是所有既有表所在之处 —— 所以升级
    不需要搬任何表：老智能体绑了根就能照常访问原有表。
    """
    owner, set_agent, ps = scope
    root = P.ensure_root_path(owner, "公共")
    P.bind_agent(f"{owner}-ag", root)
    set_agent(f"{owner}-ag")

    assert ps.data_search_path(owner) == ["public"]


@requires_platform_db
def test_data_search_path_puts_the_case_before_public(scope):
    """同时授予案件与根时，案件 schema 排在 ``public`` 之前。

    顺序有意义：PostgreSQL 用 search_path 里第一个含该表的 schema 解析非限定表名，
    因此案件专属表会遮蔽公共区同名表 —— 归档在案件下的材料比共享材料更具体。
    """
    owner, set_agent, ps = scope
    root = P.ensure_root_path(owner, "公共")
    case = P.create_path(owner, "案件一")
    for pid in (root, case):
        P.bind_agent(f"{owner}-ag", pid)
    set_agent(f"{owner}-ag")

    path = ps.data_search_path(owner)
    assert path[-1] == "public", "public 必须排最后"
    assert path[0] == P.data_schema_for(case)
    assert len(path) == 2


@requires_platform_db
def test_data_search_path_spans_several_cases(scope):
    """授予多个案件时全部列出（不是只挑一个）。

    早期实现遇到多路径会返回 None（因为当时假设"恰好一个 schema"），那会让
    绑了案件之后的智能体完全无法访问数据表 —— 而每个智能体升级后本来就绑着
    根路径，等于一绑就坏。
    """
    owner, set_agent, ps = scope
    root = P.ensure_root_path(owner, "公共")
    case1 = P.create_path(owner, "案件一")
    case2 = P.create_path(owner, "案件二")
    for pid in (root, case1, case2):
        P.bind_agent(f"{owner}-ag", pid)
    set_agent(f"{owner}-ag")

    path = ps.data_search_path(owner)
    assert set(path) == {
        P.data_schema_for(case1), P.data_schema_for(case2), "public",
    }
    assert path[-1] == "public"


@requires_platform_db
def test_data_search_path_denies_without_grants(scope):
    """没有授权时返回 None（fail-closed）。

    ``None`` 必须被调用方读作「拒绝」，绝不能读作「不加限制」—— 后者等于把
    所有案件的表全部暴露。
    """
    owner, set_agent, ps = scope
    P.ensure_root_path(owner, "公共")
    set_agent(f"{owner}-ghost")

    assert ps.data_search_path(owner) is None


@requires_platform_db
def test_data_search_path_denies_when_identity_unknown(scope):
    owner, set_agent, ps = scope
    root = P.ensure_root_path(owner, "公共")
    P.bind_agent(f"{owner}-ag", root)
    set_agent("")

    assert ps.data_search_path(owner) is None


@requires_platform_db
def test_data_search_path_denies_when_store_is_unavailable(scope, monkeypatch):
    """路径存储故障时拒绝，不放行。

    放行意味着一次数据库抖动就同时失去所有路径隔离。
    """
    owner, set_agent, ps = scope
    P.ensure_root_path(owner, "公共")
    set_agent(f"{owner}-ag")

    def boom(*_a, **_k):
        raise RuntimeError("simulated outage")

    monkeypatch.setattr(P, "agent_path_ids", boom)
    assert ps.data_search_path(owner) is None


@requires_platform_db
def test_visible_data_path_ids_denies_without_grants(scope):
    """列表用路径集合也必须 fail-closed。"""
    owner, set_agent, ps = scope
    P.ensure_root_path(owner, "公共")
    set_agent(f"{owner}-ghost")
    assert ps.visible_data_path_ids(owner) is None

    root = P.ensure_root_path(owner, "公共")
    P.bind_agent(f"{owner}-ag", root)
    set_agent(f"{owner}-ag")
    assert ps.visible_data_path_ids(owner) == {root}


def test_root_schema_constant_matches_the_data_tables_layer():
    """两处的 ROOT_SCHEMA 必须一致。

    ``path_scope`` 用它拼 search_path，``data_tables.store`` 用它决定建表/移表
    的目标 schema；两者不一致会让「根路径」在一边是 public、另一边是别的名字，
    表现为表建好了却查不到。
    """
    from qwenpaw.agents.tools import path_scope
    from qwenpaw.data_tables import store as data_store

    assert path_scope.ROOT_SCHEMA == data_store.ROOT_SCHEMA == "public"
