# -*- coding: utf-8 -*-
"""路径隔离（paths / resource_bindings / agent_path_bindings）单元测试。

这一层是「用户之内再分一层隔离」的核心：不同案件互不可见，公共知识可被多个
案件共享。测试用**已知答案构造**验证语义，而不是只断言「能跑通」—— 隔离类
逻辑最危险的失败模式是"看起来限制了、实际能绕过"。

需要真实 PostgreSQL（表在 ``platform`` schema）。无数据库时按既有 gate 跳过。
"""

from __future__ import annotations

import uuid

import pytest

from qwenpaw.platform_store import get_conn, put_conn
from qwenpaw.platform_store import paths as P

from .test_legacy_import import requires_platform_db  # noqa: E402


@pytest.fixture()
def owner() -> str:
    """一个独立账号名，测试结束清理其全部路径与绑定。"""
    name = f"pathtest-{uuid.uuid4().hex[:10]}"
    yield name
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # ``paths`` 的 CASCADE 会带走两张绑定表里的对应行。
            cur.execute(
                'DELETE FROM "platform".paths WHERE owner_username = %s',
                (name,),
            )
            cur.execute(
                'DELETE FROM "platform".agent_path_bindings WHERE agent_id LIKE %s',
                (f"{name}%",),
            )
            cur.execute(
                'DELETE FROM "platform".resource_bindings WHERE resource_id LIKE %s',
                (f"{name}%",),
            )
        conn.commit()
    except Exception:  # noqa: BLE001 - 清理失败不应掩盖测试结果
        conn.rollback()
    finally:
        put_conn(conn)


# --- 树结构 -----------------------------------------------------------------


@requires_platform_db
def test_root_path_is_created_once_and_reused(owner):
    """每账号恰好一个根路径；重复调用返回同一个 id。

    根路径是「账号原有平铺资源」的迁移落点，也是未指定父路径时的默认归属，
    因此必须稳定 —— 每次调用新建一个会让资源散落在多个根里。
    """
    first = P.ensure_root_path(owner, "默认")
    assert P.ensure_root_path(owner) == first
    assert P.ensure_root_path(owner, "换个名字") == first  # 已存在则不改名


@requires_platform_db
def test_only_one_root_per_owner_is_allowed(owner):
    """部分唯一索引保证根路径唯一。

    直接插第二条根要失败 —— 依赖应用层「记得先查」不足以保证，索引才是约束。
    """
    import psycopg2

    P.ensure_root_path(owner, "默认")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute(
                    'INSERT INTO "platform".paths '
                    "(id, owner_username, name, parent_id, is_root) "
                    "VALUES (%s, %s, %s, NULL, true)",
                    (str(uuid.uuid4()), owner, "第二个根"),
                )
        conn.rollback()
    finally:
        put_conn(conn)


@requires_platform_db
def test_nesting_is_arbitrary_depth(owner):
    """支持任意深度（案件按年份分组这类结构需要）。"""
    P.ensure_root_path(owner)
    lvl1 = P.create_path(owner, "2026年")
    lvl2 = P.create_path(owner, "第一季度", parent_id=lvl1)
    lvl3 = P.create_path(owner, "案件甲", parent_id=lvl2)

    tree = P.list_path_tree(owner)
    names = [node.name for root in tree for node in root.walk()]
    assert {"2026年", "第一季度", "案件甲"} <= set(names)

    # 父子关系正确串起来。
    node = P.path_by_id(lvl3)
    assert node["parent_id"] == lvl2


@requires_platform_db
def test_sibling_name_collision_is_rejected(owner):
    """同一层下不允许重名（不同层可以同名）。

    重名会让用户在树上无法区分两个案件，也会让「按名字找路径」变得歧义。
    """
    P.ensure_root_path(owner)
    a = P.create_path(owner, "案件一")
    with pytest.raises(P.PathScopeError, match="已存在"):
        P.create_path(owner, "案件一")
    # 换个父路径可以同名。
    other = P.create_path(owner, "其他")
    P.create_path(owner, "案件一", parent_id=other)
    _ = a


@requires_platform_db
def test_root_level_name_collision_is_also_rejected(owner):
    """根层级（parent_id 为 NULL）也要拦重名。

    默认的 UNIQUE 把 NULL 视为互不相同，因此这里必须用 ``NULLS NOT DISTINCT``，
    否则同一账号下能建出两个都叫「案件」的根级路径。
    """
    P.ensure_root_path(owner)
    P.create_path(owner, "同名")
    with pytest.raises(P.PathScopeError, match="已存在"):
        P.create_path(owner, "同名")


@requires_platform_db
def test_empty_name_is_rejected(owner):
    with pytest.raises(P.PathScopeError, match="不能为空"):
        P.create_path(owner, "   ")


@requires_platform_db
def test_paths_are_isolated_between_accounts(owner):
    """一个账号看不到另一个账号的路径树。"""
    P.ensure_root_path(owner)
    P.create_path(owner, "我的案件")

    stranger = f"{owner}-stranger"
    assert P.list_path_tree(stranger) == []
    # 也不能操作别人的路径。
    with pytest.raises(P.PathScopeError):
        P.create_path(stranger, "偷来的", parent_id=P.ensure_root_path(owner))


# --- 资源归属 ---------------------------------------------------------------


@requires_platform_db
def test_resource_belongs_to_exactly_one_path(owner):
    """一个资源只能属于一个路径；再次绑定是**移动**而非新增归属。

    若允许同时属于多个路径，为某个案件修改材料会静默影响另一个案件 ——
    正是本功能要消除的耦合。共享应通过"共同的父路径"实现，而不是复制归属。
    """
    P.ensure_root_path(owner)
    a = P.create_path(owner, "案件一")
    b = P.create_path(owner, "案件二")
    resource = f"{owner}-doc"

    P.bind_resource(a, P.KIND_KNOWLEDGE_SPACE, resource)
    assert P.path_of_resource(P.KIND_KNOWLEDGE_SPACE, resource) == a

    P.bind_resource(b, P.KIND_KNOWLEDGE_SPACE, resource)
    assert P.path_of_resource(P.KIND_KNOWLEDGE_SPACE, resource) == b
    # 旧路径下不再有它。
    assert P.path_of_resource(P.KIND_KNOWLEDGE_SPACE, resource) != a
    assert all(
        item["resource_id"] != resource
        for item in P.resources_in_path(a)
    )


@requires_platform_db
def test_unknown_resource_kind_is_rejected(owner):
    """资源类型是封闭集合，防止拼错后产生永远查不到的绑定。"""
    path_id = P.ensure_root_path(owner)
    with pytest.raises(P.PathScopeError, match="未知的资源类型"):
        P.bind_resource(path_id, "knowledge_spase", "x")  # 拼错


@requires_platform_db
def test_binding_to_missing_path_is_rejected(owner):
    with pytest.raises(P.PathScopeError, match="路径不存在"):
        P.bind_resource(str(uuid.uuid4()), P.KIND_DATA_TABLE, "x")


# --- 智能体授权与隔离 -------------------------------------------------------


@requires_platform_db
def test_agent_without_binding_sees_nothing(owner):
    """空绑定 = 什么都看不到（fail-closed）。

    这是刻意的选择：新增路径不会自动对既有智能体可见，否则「案件隔离」永远
    不会真正生效。升级时由迁移给现有智能体补绑定以保持原有可见范围。
    """
    P.ensure_root_path(owner)
    assert P.allowed_resource_ids(f"{owner}-agent", P.KIND_KNOWLEDGE_SPACE) == []


@requires_platform_db
def test_two_cases_cannot_see_each_other(owner):
    """核心场景：两个案件互不可见，但都能看到公共知识。"""
    P.ensure_root_path(owner)
    common = P.create_path(owner, "公共知识")
    case1 = P.create_path(owner, "案件一")
    case2 = P.create_path(owner, "案件二")

    P.bind_resource(common, P.KIND_KNOWLEDGE_SPACE, f"{owner}-侦查手段")
    P.bind_resource(case1, P.KIND_KNOWLEDGE_SPACE, f"{owner}-案件一台账")
    P.bind_resource(case2, P.KIND_KNOWLEDGE_SPACE, f"{owner}-案件二台账")

    for pid in (common, case1):
        P.bind_agent(f"{owner}-ag1", pid)
    for pid in (common, case2):
        P.bind_agent(f"{owner}-ag2", pid)

    seen1 = set(P.allowed_resource_ids(f"{owner}-ag1", P.KIND_KNOWLEDGE_SPACE))
    seen2 = set(P.allowed_resource_ids(f"{owner}-ag2", P.KIND_KNOWLEDGE_SPACE))

    assert f"{owner}-案件一台账" in seen1
    assert f"{owner}-案件二台账" not in seen1, "案件一不得看到案件二"
    assert f"{owner}-案件二台账" in seen2
    assert f"{owner}-案件一台账" not in seen2, "案件二不得看到案件一"
    # 公共知识两边都有。
    assert f"{owner}-侦查手段" in seen1 and f"{owner}-侦查手段" in seen2


@requires_platform_db
def test_moving_a_resource_changes_visibility_immediately(owner):
    """资源移到别的路径后，未授权该路径的智能体立刻失去访问。

    这条保证「权限跟着路径走」而不是「跟资源走」—— 否则移动材料后还得记得
    单独调整授权，迟早漏掉。
    """
    P.ensure_root_path(owner)
    case1 = P.create_path(owner, "案件一")
    case2 = P.create_path(owner, "案件二")
    resource = f"{owner}-doc"

    P.bind_resource(case2, P.KIND_KNOWLEDGE_SPACE, resource)
    P.bind_agent(f"{owner}-ag1", case1)
    assert P.allowed_resource_ids(f"{owner}-ag1", P.KIND_KNOWLEDGE_SPACE) == []

    P.bind_resource(case1, P.KIND_KNOWLEDGE_SPACE, resource)
    assert P.allowed_resource_ids(f"{owner}-ag1", P.KIND_KNOWLEDGE_SPACE) == [resource]

    P.bind_resource(case2, P.KIND_KNOWLEDGE_SPACE, resource)
    assert P.allowed_resource_ids(f"{owner}-ag1", P.KIND_KNOWLEDGE_SPACE) == []


@requires_platform_db
def test_granting_a_parent_does_not_expose_children(owner):
    """父路径不自动包含子路径的资源。

    若父路径隐含子路径，把根路径授权给一个智能体就等于暴露账号下全部案件 ——
    树会退化成它要替代的平铺桶。
    """
    root = P.ensure_root_path(owner)
    case = P.create_path(owner, "案件一")
    P.bind_resource(root, P.KIND_KNOWLEDGE_SPACE, f"{owner}-根上的")
    P.bind_resource(case, P.KIND_KNOWLEDGE_SPACE, f"{owner}-案件里的")

    P.bind_agent(f"{owner}-ag", root)
    seen = P.allowed_resource_ids(f"{owner}-ag", P.KIND_KNOWLEDGE_SPACE)
    assert seen == [f"{owner}-根上的"]


@requires_platform_db
def test_allowed_resources_are_filtered_by_kind(owner):
    """两种资源类型不混淆：知识空间查询不会带出数据表。"""
    path_id = P.ensure_root_path(owner)
    P.bind_resource(path_id, P.KIND_KNOWLEDGE_SPACE, f"{owner}-space")
    P.bind_resource(path_id, P.KIND_DATA_TABLE, f"{owner}-table")
    P.bind_agent(f"{owner}-ag", path_id)

    assert P.allowed_resource_ids(f"{owner}-ag", P.KIND_KNOWLEDGE_SPACE) == [
        f"{owner}-space",
    ]
    assert P.allowed_resource_ids(f"{owner}-ag", P.KIND_DATA_TABLE) == [
        f"{owner}-table",
    ]


@requires_platform_db
def test_unbind_agent_removes_access(owner):
    path_id = P.ensure_root_path(owner)
    P.bind_resource(path_id, P.KIND_DATA_TABLE, f"{owner}-t")
    P.bind_agent(f"{owner}-ag", path_id)
    assert P.allowed_resource_ids(f"{owner}-ag", P.KIND_DATA_TABLE) == [f"{owner}-t"]

    P.unbind_agent(f"{owner}-ag", path_id)
    assert P.allowed_resource_ids(f"{owner}-ag", P.KIND_DATA_TABLE) == []


@requires_platform_db
def test_binding_an_agent_twice_is_idempotent(owner):
    path_id = P.ensure_root_path(owner)
    P.bind_agent(f"{owner}-ag", path_id)
    P.bind_agent(f"{owner}-ag", path_id)
    assert P.agent_path_ids(f"{owner}-ag") == [path_id]


@requires_platform_db
def test_agent_binding_requires_an_existing_path(owner):
    with pytest.raises(P.PathScopeError):
        P.bind_agent(f"{owner}-ag", str(uuid.uuid4()))


@requires_platform_db
def test_grant_all_existing_paths_covers_the_whole_tree(owner):
    """升级用的一次性授权：把账号当前所有路径都给某个智能体。

    这是「行为在升级当天不变」的保证 —— 迁移前该智能体能看账号全部资源，
    迁移后能看到全部路径下的资源，集合相等。
    """
    root = P.ensure_root_path(owner)
    child = P.create_path(owner, "案件一")
    grand = P.create_path(owner, "子案件", parent_id=child)
    P.bind_resource(grand, P.KIND_KNOWLEDGE_SPACE, f"{owner}-deep")

    granted = P.grant_all_existing_paths(owner, f"{owner}-ag")
    assert granted == 3
    assert set(P.agent_path_ids(f"{owner}-ag")) == {root, child, grand}
    # 深层资源也随之可见（因为它的路径被授权了）。
    assert P.allowed_resource_ids(f"{owner}-ag", P.KIND_KNOWLEDGE_SPACE) == [
        f"{owner}-deep",
    ]


# --- 移动 / 删除的约束 ------------------------------------------------------


@requires_platform_db
def test_path_cannot_be_moved_into_its_own_subtree(owner):
    """阻止环形引用。

    把节点拖进自己的子树会让那棵子树从树上脱落（自己成为自己的祖先），
    因此这个检查不能省 —— UI 不可信，手工调 API 更不可信。
    """
    P.ensure_root_path(owner)
    parent = P.create_path(owner, "父")
    child = P.create_path(owner, "子", parent_id=parent)
    grand = P.create_path(owner, "孙", parent_id=child)

    with pytest.raises(P.PathScopeError, match="子树"):
        P.move_path(parent, grand)
    with pytest.raises(P.PathScopeError, match="它自己"):
        P.move_path(parent, parent)

    # 移到自己当前的父级是允许的（等价于没变）。
    P.move_path(child, parent)
    assert P.path_by_id(child)["parent_id"] == parent


@requires_platform_db
def test_moving_across_parents_works(owner):
    P.ensure_root_path(owner)
    a = P.create_path(owner, "甲")
    b = P.create_path(owner, "乙")
    node = P.create_path(owner, "节点", parent_id=a)

    P.move_path(node, b)
    assert P.path_by_id(node)["parent_id"] == b


@requires_platform_db
def test_root_path_cannot_be_moved(owner):
    root = P.ensure_root_path(owner)
    with pytest.raises(P.PathScopeError, match="根路径不能移动"):
        P.move_path(root, None)


@requires_platform_db
def test_renaming_to_a_sibling_name_is_rejected(owner):
    P.ensure_root_path(owner)
    P.create_path(owner, "甲")
    b = P.create_path(owner, "乙")
    with pytest.raises(P.PathScopeError, match="已存在"):
        P.rename_path(b, "甲")


@requires_platform_db
def test_only_empty_paths_can_be_deleted(owner):
    """只允许删空路径：一次误操作不该丢掉整个案件。

    ``ON DELETE CASCADE`` 会让绑定随之消失，而案件目录通常含大量材料 ——
    要求先移走内容，把这个破坏性动作变成显式的两步。
    """
    root = P.ensure_root_path(owner)
    case = P.create_path(owner, "案件一")
    P.bind_resource(case, P.KIND_KNOWLEDGE_SPACE, f"{owner}-doc")
    with pytest.raises(P.PathScopeError, match="还有内容"):
        P.delete_path(case)

    # 移走资源后可以删。
    P.unbind_resource(P.KIND_KNOWLEDGE_SPACE, f"{owner}-doc")
    P.delete_path(case)
    assert P.path_by_id(case) is None

    # 有子路径也不算空 —— 用一个非根路径来验（根路径会先撞上「根不能删」，
    # 那条规则优先级更高，会把这条断言盖掉）。
    case2 = P.create_path(owner, "案件二")
    P.create_path(owner, "子案件", parent_id=case2)
    with pytest.raises(P.PathScopeError, match="还有内容"):
        P.delete_path(case2)
    _ = root


@requires_platform_db
def test_root_path_cannot_be_deleted(owner):
    root = P.ensure_root_path(owner)
    with pytest.raises(P.PathScopeError, match="根路径不能删除"):
        P.delete_path(root)


@requires_platform_db
def test_deleting_a_path_removes_its_agent_bindings(owner):
    """CASCADE 清理绑定，不留孤儿记录。"""
    P.ensure_root_path(owner)
    case = P.create_path(owner, "案件")
    P.bind_agent(f"{owner}-ag", case)
    assert P.agents_for_path(case) == [f"{owner}-ag"]

    P.delete_path(case)
    assert P.agents_for_path(case) == []
    assert case not in P.agent_path_ids(f"{owner}-ag")


# --- schema 名派生 ----------------------------------------------------------


def test_data_schema_name_is_deterministic_and_safe():
    """数据表 schema 名必须确定、唯一、且只含标识符安全字符。

    schema 名无法用绑定参数，只能拼进 DDL 与 ``SET search_path`` —— 因此它
    绝不能带引号、空格或分隔符。确定性保证多进程算出的名字一致。
    """
    path_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    name = P.data_schema_for(path_id)
    assert name == P.data_schema_for(path_id), "必须确定"
    assert name.startswith(P.DATA_SCHEMA_PREFIX)
    assert all(ch.isalnum() or ch == "_" for ch in name)
    assert name == name.lower()
    # 不同路径得到不同 schema。
    other = "a1b2c3d4-e5f6-7890-abcd-ef1234567891"
    assert P.data_schema_for(other) != name


def test_data_schema_name_rejects_non_uuid_input():
    """非 UUID 输入要报错，而不是产出一个可能碰撞或危险的 schema 名。"""
    with pytest.raises(P.PathScopeError, match="UUID"):
        P.data_schema_for("not-a-uuid")
    with pytest.raises(P.PathScopeError, match="UUID"):
        P.data_schema_for('x"; DROP SCHEMA public; --')


def test_data_schema_name_cannot_be_injected():
    """即使输入带引号，也只会被过滤成安全字符（且长度不足时报错）。"""
    with pytest.raises(P.PathScopeError):
        P.data_schema_for("'; DROP TABLE x; --")


# --- 公共区 / 隔离区模型（升级行为不变的关键）------------------------------


@requires_platform_db
def test_unbound_resource_falls_back_to_the_root(owner):
    """未绑定的资源视为属于根路径。

    这是「升级后行为不变」的实现基础：升级前所有资源都没有绑定，把它们一律
    当作在根路径下，就不需要在升级时枚举三个不同位置的资源（知识空间在后面
    的 HTTP 服务里、数据表在各账号库里、spaces 在知识库中）。枚举一旦漏掉
    一项，那份材料就会从所有智能体视野里消失。
    """
    root = P.ensure_root_path(owner, "公共")
    assert P.effective_path_of(
        P.KIND_KNOWLEDGE_SPACE, f"{owner}-未绑定", owner,
    ) == root


@requires_platform_db
def test_existing_agent_keeps_seeing_public_material(owner):
    """升级后老智能体仍能看到公共（未绑定）材料 —— 与升级前一致。"""
    root = P.ensure_root_path(owner, "公共")
    P.bind_agent(f"{owner}-ag", root)
    assert P.is_visible_to_agent(
        agent_id=f"{owner}-ag",
        resource_kind=P.KIND_KNOWLEDGE_SPACE,
        resource_id=f"{owner}-公共资料",
        owner_username=owner,
    )


@requires_platform_db
def test_binding_material_to_a_case_hides_it_from_old_agents(owner):
    """一旦把材料绑到案件路径，只绑了根路径的老智能体立刻看不到它。

    这正是「公共 → 隔离」的操作方式：隔离不是靠开关，而是靠把材料移进
    子路径。
    """
    root = P.ensure_root_path(owner, "公共")
    case = P.create_path(owner, "案件一")
    P.bind_agent(f"{owner}-ag-old", root)

    resource = f"{owner}-材料"
    # 未绑定时可见（公共）。
    assert P.is_visible_to_agent(
        agent_id=f"{owner}-ag-old", resource_kind=P.KIND_KNOWLEDGE_SPACE,
        resource_id=resource, owner_username=owner,
    )
    # 绑到案件后就不可见了。
    P.bind_resource(case, P.KIND_KNOWLEDGE_SPACE, resource)
    assert not P.is_visible_to_agent(
        agent_id=f"{owner}-ag-old", resource_kind=P.KIND_KNOWLEDGE_SPACE,
        resource_id=resource, owner_username=owner,
    )


@requires_platform_db
def test_case_agent_sees_both_public_and_its_own_case(owner):
    """案件智能体 = 根（公共）+ 本案路径，两者都能看到。"""
    root = P.ensure_root_path(owner, "公共")
    case = P.create_path(owner, "案件一")
    P.bind_resource(case, P.KIND_KNOWLEDGE_SPACE, f"{owner}-案件材料")
    P.bind_agent(f"{owner}-ag", root)
    P.bind_agent(f"{owner}-ag", case)

    def visible(rid: str) -> bool:
        return P.is_visible_to_agent(
            agent_id=f"{owner}-ag", resource_kind=P.KIND_KNOWLEDGE_SPACE,
            resource_id=rid, owner_username=owner,
        )

    assert visible(f"{owner}-公共资料")
    assert visible(f"{owner}-案件材料")


@requires_platform_db
def test_grant_root_to_agent_is_the_upgrade_step(owner):
    """升级对每个智能体做的唯一一件事：授予根路径。

    只授根、不授子路径 —— 这样从第一个新建案件开始隔离就生效，而升级当天
    的可见范围与升级前完全相等。
    """
    root = P.ensure_root_path(owner, "默认")
    case = P.create_path(owner, "案件一")
    granted = P.grant_root_to_agent(owner, f"{owner}-ag")

    assert granted == root
    assert P.agent_path_ids(f"{owner}-ag") == [root]
    assert case not in P.agent_path_ids(f"{owner}-ag")


def test_migration_never_raises_when_config_is_unavailable(monkeypatch):
    """枚举智能体失败时只记录、不抛出 —— 它跑在启动路径上。"""
    from qwenpaw.platform_store import path_migration

    def boom():
        raise RuntimeError("simulated config failure")

    monkeypatch.setattr(path_migration, "_iter_account_agents", boom)
    report = path_migration.migrate_into_path_scoping()
    assert report.errors
    assert report.agents_granted == 0


def test_migration_isolates_a_failing_account(monkeypatch):
    """单个账号失败不影响其余账号，也不会中断启动。"""
    from qwenpaw.platform_store import path_migration

    monkeypatch.setattr(
        path_migration,
        "_iter_account_agents",
        lambda: {"acct-a": ["ag1"], "acct-b": ["ag2"]},
    )

    def ensure_root(owner, name="默认"):
        if owner == "acct-a":
            raise RuntimeError("simulated failure for acct-a")
        return "root-b"

    granted: list[str] = []
    monkeypatch.setattr(path_migration.path_store, "ensure_root_path", ensure_root)
    monkeypatch.setattr(
        path_migration.path_store,
        "grant_root_to_agent",
        lambda owner, agent: granted.append(agent) or "root-b",
    )

    report = path_migration.migrate_into_path_scoping()
    assert report.accounts == 1  # 只有 acct-b 成功
    assert granted == ["ag2"]  # acct-a 被隔离掉
    assert any("acct-a" in err for err in report.errors)
