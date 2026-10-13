# -*- coding: utf-8 -*-
"""智能体生命周期与路径授权的一致性。

授权的键是 agent id，所以 agent 的创建与删除都必须同时维护它，否则会出现两种
「静默错」：

* **新建的智能体什么都读不到** —— 路径判定是 fail-closed 的，一个没有任何授权的
  智能体检索任何知识/数据都会被拒。用户会觉得「智能体坏了」，而原因藏在另一个
  模块里。因此新建（含复制）默认授予账号的公共区（根路径）。
* **删除的智能体留下孤儿授权** —— 授权行仍在，且 ``*_default`` 这类 id 是按账号
  推导出来的、可复用，新智能体可能因此静默继承旧智能体对某些案件的访问权。

这里直接验证这两条不变量，而不是走完整的 HTTP 流程（那两个函数需要较多运行时
上下文，且本测试关注的是与路径存储的契约）。
"""

from __future__ import annotations

import uuid

import pytest

from qwenpaw.platform_store import get_conn, put_conn
from qwenpaw.platform_store import paths as P

from tests.unit.platform_store.test_legacy_import import (  # noqa: E402
    requires_platform_db,
)


@pytest.fixture()
def owner():
    """一个临时账号，结束后清理它的路径与授权。"""
    name = f"lifecycle-{uuid.uuid4().hex[:10]}"
    yield name
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                'DELETE FROM "platform".paths WHERE owner_username = %s', (name,),
            )
            cur.execute(
                'DELETE FROM "platform".agent_path_bindings WHERE agent_id LIKE %s',
                (f"{name}%",),
            )
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
    finally:
        put_conn(conn)


@requires_platform_db
def test_grant_root_is_idempotent(owner):
    """重复授予根路径不报错、也不产生重复行。

    创建与复制都会调用它，且升级迁移也会调用；重复调用必须安全。
    """
    agent = f"{owner}-ag"
    first = P.grant_root_to_agent(owner, agent)
    second = P.grant_root_to_agent(owner, agent)
    assert first == second
    assert P.agent_path_ids(agent) == [first]


@requires_platform_db
def test_unbinding_all_grants_leaves_no_trace(owner):
    """删除智能体时逐个解绑，最终不留任何授权行。

    ``unbind_agent`` 是删除路径用的既有函数；生命周期清理复用它，因此这里确认
    它在「全删」语义下确实把该 agent 的授权清空（而不是只删掉一条）。
    """
    agent = f"{owner}-ag"
    root = P.ensure_root_path(owner, "公共")
    case = P.create_path(owner, "案件一")
    P.bind_agent(agent, root)
    P.bind_agent(agent, case)
    assert set(P.agent_path_ids(agent)) == {root, case}

    for path_id in list(P.agent_path_ids(agent)):
        P.unbind_agent(agent, path_id)

    assert P.agent_path_ids(agent) == []
    # The paths themselves must survive: only the agent's access went away.
    assert P.path_by_id(case) is not None


@requires_platform_db
def test_unbinding_an_agent_does_not_touch_the_path(owner):
    """解绑智能体只删授权，保留路径本身。

    两个方向的清理必须互不干扰：删智能体不应让案件路径跟着消失（那是用户自己的
    资料结构），否则一次「删掉一个智能体」会意外毁掉案件的组织方式。
    """
    agent = f"{owner}-ag"
    case = P.create_path(owner, "案件一")
    P.bind_agent(agent, case)

    P.unbind_agent(agent, case)
    assert P.path_by_id(case) is not None, "解绑智能体不得影响路径"
    assert P.agent_path_ids(agent) == []


@requires_platform_db
def test_a_path_with_a_granted_agent_is_not_deletable(owner):
    """挂了智能体的路径不能被删除（需要先解绑）。

    若允许删除，级联会静默撤掉该智能体对这个案件的访问，而使用者看不到任何
    提示 —— 表现为「检索突然查不到了」。所以删除前必须显式解绑。
    """
    agent = f"{owner}-ag"
    case = P.create_path(owner, "案件一")
    P.bind_agent(agent, case)

    with pytest.raises(P.PathScopeError):
        P.delete_path(case)

    # After unbinding, the path becomes deletable again.
    P.unbind_agent(agent, case)
    P.delete_path(case)
    assert P.path_by_id(case) is None
