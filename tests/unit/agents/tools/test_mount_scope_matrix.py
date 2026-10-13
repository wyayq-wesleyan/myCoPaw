# -*- coding: utf-8 -*-
"""聊天界面「挂载路径」的完整行为矩阵。

这一份是**面向使用者的验收测试**，逐条覆盖实际会遇到的场景，而不是只验证函数
「跑通了」。重点盯住两类真实缺陷：

1. **没挂载却查到了** —— 越界。用户把两个案件分开挂到不同智能体，就是怕串味；
   一旦没挂载也能读到，隔离等于不存在。
2. **挂了却没查到** —— 失效。用户挂了路径，结果智能体说「没有相关内容」，这时
   很难判断是「本来就没有」还是「挂载没生效」。所以「挂了必然查得到」必须和
   「没挂必然查不到」一样被锁死。

矩阵按「路径类型 × 资源位置 × 挂载组合」展开，知识侧与数据侧分别覆盖：

| 场景 | 挂载 | 资源在哪 | 期望 |
|---|---|---|---|
| 完全没挂 | 无 | 任何位置 | 看不到（且提示应先挂载，不是「没有结果」） |
| 只挂公共区 | root | 未归类 | 看得到 |
| 只挂公共区 | root | 案件里 | 看不到（不越界） |
| 只挂案件 | case | 该案件 | 看得到 |
| 只挂案件 | case | 别的案件 | 看不到 |
| 只挂案件 | case | 未归类（公共区） | 看不到（没挂根就看不到公共） |
| 挂案件 + 公共区 | case+root | 两者都 | 都看得到 |
| 挂 A + B | A+B | A 与 B | 都看得到，C 看不到 |
| 挂载变更后 | 改挂 | — | 立即生效，无需重启 |
"""

from __future__ import annotations

import uuid

import pytest

from qwenpaw.agents.tools import path_scope
from qwenpaw.platform_store import get_conn, put_conn
from qwenpaw.platform_store import paths as P

from ...platform_store.test_legacy_import import (  # noqa: E402
    requires_platform_db,
)


@pytest.fixture()
def scope(monkeypatch):
    """临时账号 + 可切换的智能体身份，结束后清理。

    ``data_search_path`` / ``visible_document_ids`` 都通过 ``current_agent_id()``
    解析「谁在问」，所以这里替换它来扮演不同智能体 —— 与三个强制点用的是同一个
    入口，因此测的就是真实判定路径，而不是另写一套等价逻辑。
    """
    owner = f"mount-{uuid.uuid4().hex[:10]}"
    state = {"agent": ""}

    def set_agent(agent_id: str) -> None:
        state["agent"] = agent_id
        monkeypatch.setattr(path_scope, "current_agent_id", lambda: state["agent"])

    yield owner, set_agent

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                'DELETE FROM "platform".paths WHERE owner_username LIKE %s',
                (f"{owner}%",),
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


# --- 知识空间 ---------------------------------------------------------------


@requires_platform_db
def test_no_grant_reads_nothing_and_says_so(scope):
    """完全没挂载：什么都读不到（fail-closed）。

    这是最重要的一条 —— 新智能体若因为漏掉默认挂载而「什么都读不到」，用户会以为
    智能体坏了；但方向绝不能反过来（没挂也能读），否则隔离形同不存在。
    """
    owner, set_agent = scope
    P.ensure_root_path(owner, "公共")
    set_agent(f"{owner}-ag")

    assert path_scope.visible_document_ids(owner, ["doc-a", "doc-b"]) == []


@requires_platform_db
def test_granted_case_sees_its_document(scope):
    """挂了某案件 → 该案件的文件查得到（对应「挂了没查到」）。"""
    owner, set_agent = scope
    case = P.create_path(owner, "案件一")
    P.bind_resource(case, P.KIND_KNOWLEDGE_DOCUMENT, f"{owner}-案内")
    P.bind_agent(f"{owner}-ag", case)
    set_agent(f"{owner}-ag")

    assert path_scope.visible_document_ids(owner, [f"{owner}-案内"]) == [f"{owner}-案内"]


@requires_platform_db
def test_granted_case_does_not_see_another_case(scope):
    """挂了案件A → 看不到案件B（对应「没挂载却查到了」）。"""
    owner, set_agent = scope
    case_a = P.create_path(owner, "案件A")
    case_b = P.create_path(owner, "案件B")
    P.bind_resource(case_b, P.KIND_KNOWLEDGE_DOCUMENT, f"{owner}-B的材料")
    P.bind_agent(f"{owner}-ag", case_a)
    set_agent(f"{owner}-ag")

    assert path_scope.visible_document_ids(owner, [f"{owner}-B的材料"]) == []


@requires_platform_db
def test_granted_case_does_not_see_public_area(scope):
    """只挂案件、没挂公共区 → 连未归类的公共材料也看不到。

    这是刻意的严格语义：挂载就是全部可达范围。若「案件 + 隐式公共」会自动合并，
    用户就无法用「不挂根」来表达「这个智能体不要读公共材料」。
    """
    owner, set_agent = scope
    case = P.create_path(owner, "案件A")
    P.bind_agent(f"{owner}-ag", case)
    set_agent(f"{owner}-ag")

    # ``未归类`` 没有任何绑定，即公共区。
    assert path_scope.visible_document_ids(owner, [f"{owner}-未归类"]) == []


@requires_platform_db
def test_root_grant_sees_public_but_not_cases(scope):
    """挂了公共区 → 看得到未归类的，看不到案件材料。"""
    owner, set_agent = scope
    root = P.ensure_root_path(owner, "公共")
    case = P.create_path(owner, "案件A")
    P.bind_resource(case, P.KIND_KNOWLEDGE_DOCUMENT, f"{owner}-案内")
    P.bind_agent(f"{owner}-ag", root)
    set_agent(f"{owner}-ag")

    visible = path_scope.visible_document_ids(owner, [f"{owner}-未归类", f"{owner}-案内"])
    assert visible == [f"{owner}-未归类"]


@requires_platform_db
def test_mounting_case_plus_root_sees_both(scope):
    """同时挂案件与公共区 → 两者都看得到。"""
    owner, set_agent = scope
    root = P.ensure_root_path(owner, "公共")
    case = P.create_path(owner, "案件A")
    P.bind_resource(case, P.KIND_KNOWLEDGE_DOCUMENT, f"{owner}-案内")
    for pid in (root, case):
        P.bind_agent(f"{owner}-ag", pid)
    set_agent(f"{owner}-ag")

    visible = set(path_scope.visible_document_ids(
        owner, [f"{owner}-未归类", f"{owner}-案内"],
    ))
    assert visible == {f"{owner}-未归类", f"{owner}-案内"}


@requires_platform_db
def test_multiple_cases_unite_and_exclude_the_rest(scope):
    """挂 A+B → A、B 都看得到，C 看不到（多挂载的核心场景）。"""
    owner, set_agent = scope
    a = P.create_path(owner, "案件A")
    b = P.create_path(owner, "案件B")
    c = P.create_path(owner, "案件C")
    P.bind_resource(a, P.KIND_KNOWLEDGE_DOCUMENT, f"{owner}-A")
    P.bind_resource(b, P.KIND_KNOWLEDGE_DOCUMENT, f"{owner}-B")
    P.bind_resource(c, P.KIND_KNOWLEDGE_DOCUMENT, f"{owner}-C")
    for pid in (a, b):
        P.bind_agent(f"{owner}-ag", pid)
    set_agent(f"{owner}-ag")

    visible = set(path_scope.visible_document_ids(owner, [f"{owner}-A", f"{owner}-B", f"{owner}-C"]))
    assert visible == {f"{owner}-A", f"{owner}-B"}


@requires_platform_db
def test_unmounting_takes_effect_immediately(scope):
    """取消挂载后立即读不到 —— 不需要重启或重新建会话。

    挂载是每次检索时从数据库解析的，所以「改了没生效」不能靠缓存来解释。
    """
    owner, set_agent = scope
    case = P.create_path(owner, "案件A")
    P.bind_resource(case, P.KIND_KNOWLEDGE_DOCUMENT, f"{owner}-材料")
    P.bind_agent(f"{owner}-ag", case)
    set_agent(f"{owner}-ag")
    assert path_scope.visible_document_ids(owner, [f"{owner}-材料"]) == [f"{owner}-材料"]

    P.unbind_agent(f"{owner}-ag", case)
    assert path_scope.visible_document_ids(owner, [f"{owner}-材料"]) == []


@requires_platform_db
def test_moving_a_document_between_cases_follows_immediately(scope):
    """文件被移到别的案件后，原案件的智能体立即读不到。"""
    owner, set_agent = scope
    a = P.create_path(owner, "案件A")
    b = P.create_path(owner, "案件B")
    doc = f"{owner}-材料"
    P.bind_resource(a, P.KIND_KNOWLEDGE_DOCUMENT, doc)
    P.bind_agent(f"{owner}-ag", a)
    set_agent(f"{owner}-ag")
    assert path_scope.visible_document_ids(owner, [doc]) == [doc]

    P.bind_resource(b, P.KIND_KNOWLEDGE_DOCUMENT, doc)
    assert path_scope.visible_document_ids(owner, [doc]) == []


@requires_platform_db
def test_unknown_agent_identity_is_denied(scope):
    """身份解析不出来时判为拒绝，而不是当成「没有限制」。"""
    owner, set_agent = scope
    root = P.ensure_root_path(owner, "公共")
    P.bind_agent(f"{owner}-ag", root)
    set_agent("")  # 拿不到身份

    assert path_scope.visible_document_ids(owner, ["doc-a"]) == []


# --- 数据空间 ---------------------------------------------------------------


@requires_platform_db
def test_data_no_grant_denies_the_whole_query_tool(scope):
    """完全没挂载数据路径 → ``data_search_path`` 返回 None（调用方据此拒绝）。

    返回 None 而不是空列表是刻意的：调用方必须能区分「没有任何路径」与「路径为空
    但存在」，前者必须拒绝，不能退化成「不受限」。
    """
    owner, set_agent = scope
    P.ensure_root_path(owner, "公共")
    set_agent(f"{owner}-ag")

    assert path_scope.data_search_path(owner) is None


@requires_platform_db
def test_data_grant_is_the_cases_schema(scope):
    """挂了案件 → search_path 指向该案件的 schema（表物理就在那里）。"""
    owner, set_agent = scope
    case = P.create_path(owner, "案件A")
    P.bind_agent(f"{owner}-ag", case)
    set_agent(f"{owner}-ag")

    assert path_scope.data_search_path(owner) == [P.data_schema_for(case)]


@requires_platform_db
def test_data_root_grant_maps_to_public_schema(scope):
    """挂公共区 → search_path 用 ``public``（未归类表所在处）。"""
    owner, set_agent = scope
    root = P.ensure_root_path(owner, "公共")
    P.bind_agent(f"{owner}-ag", root)
    set_agent(f"{owner}-ag")

    assert path_scope.data_search_path(owner) == ["public"]


@requires_platform_db
def test_data_multiple_grants_put_the_case_before_public(scope):
    """挂案件 + 公共区 → 案件 schema 排在 ``public`` **之前**。

    顺序有意义：PostgreSQL 按 search_path 顺序解析未限定名，案件在前意味着同名表
    时案件版本优先 —— 案件材料比公共材料更具体，这是直觉上的优先级。
    """
    owner, set_agent = scope
    root = P.ensure_root_path(owner, "公共")
    case = P.create_path(owner, "案件A")
    for pid in (root, case):
        P.bind_agent(f"{owner}-ag", pid)
    set_agent(f"{owner}-ag")

    assert path_scope.data_search_path(owner) == [P.data_schema_for(case), "public"]


@requires_platform_db
def test_data_two_cases_yield_both_schemas_and_neither_of_the_third(scope):
    """挂 A+B → search_path 恰为 A、B 两个 schema，绝不含 C。"""
    owner, set_agent = scope
    a = P.create_path(owner, "案件A")
    b = P.create_path(owner, "案件B")
    c = P.create_path(owner, "案件C")
    for pid in (a, b):
        P.bind_agent(f"{owner}-ag", pid)
    set_agent(f"{owner}-ag")

    search_path = path_scope.data_search_path(owner)
    assert set(search_path) == {P.data_schema_for(a), P.data_schema_for(b)}
    assert P.data_schema_for(c) not in search_path


@requires_platform_db
def test_data_visible_path_ids_match_the_grants(scope):
    """列表用的「可见路径 id」必须与授权完全一致。

    列表与查询若各有一套口径，就会出现「列表里有、点进去查不到」或反之 —— 这类
    不一致最难排查，所以两者都从同一处授权推导。
    """
    owner, set_agent = scope
    a = P.create_path(owner, "案件A")
    b = P.create_path(owner, "案件B")
    P.bind_agent(f"{owner}-ag", a)
    set_agent(f"{owner}-ag")

    assert path_scope.visible_data_path_ids(owner) == {a}
    assert b not in (path_scope.visible_data_path_ids(owner) or set())


@requires_platform_db
def test_data_unmounting_removes_the_schema_immediately(scope):
    """取消挂载 → schema 立即从 search_path 消失。"""
    owner, set_agent = scope
    case = P.create_path(owner, "案件A")
    P.bind_agent(f"{owner}-ag", case)
    set_agent(f"{owner}-ag")
    assert path_scope.data_search_path(owner) == [P.data_schema_for(case)]

    P.unbind_agent(f"{owner}-ag", case)
    assert path_scope.data_search_path(owner) is None


@requires_platform_db
def test_two_agents_of_one_account_do_not_share_scope(scope):
    """同一账号的两个智能体各自独立 —— 挂载是按智能体的，不是按账号的。

    这是「案件互不可见」的关键：同一个人可以有多个智能体，分别处理不同案件。
    """
    owner, set_agent = scope
    a = P.create_path(owner, "案件A")
    b = P.create_path(owner, "案件B")
    P.bind_resource(a, P.KIND_KNOWLEDGE_DOCUMENT, f"{owner}-A")
    P.bind_resource(b, P.KIND_KNOWLEDGE_DOCUMENT, f"{owner}-B")
    P.bind_agent(f"{owner}-agentA", a)
    P.bind_agent(f"{owner}-agentB", b)

    set_agent(f"{owner}-agentA")
    assert path_scope.visible_document_ids(owner, [f"{owner}-A", f"{owner}-B"]) == [f"{owner}-A"]
    assert path_scope.data_search_path(owner) == [P.data_schema_for(a)]

    set_agent(f"{owner}-agentB")
    assert path_scope.visible_document_ids(owner, [f"{owner}-A", f"{owner}-B"]) == [f"{owner}-B"]
    assert path_scope.data_search_path(owner) == [P.data_schema_for(b)]
