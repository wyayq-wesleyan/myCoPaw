# -*- coding: utf-8 -*-
"""聊天页顶部的「按智能体授权路径」接口。

这条链路决定某一智能体能读到哪些路径下的知识/数据，而且入口在聊天页（普通用户
可达），所以两个方向都要守住：

1. **归属校验**：只能给本账号自己的智能体写授权。否则一个账号可以把别人的智能体
   接到自己的案件上（或反向），是越权。
2. **整体替换**：勾选状态与智能体实际可达必须一致。接口按集合整体写入，避免
   逐条增删在中途失败时留下「界面上没勾、实际能读」的状态。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from qwenpaw.app.routers import paths as api
from qwenpaw.platform_store import get_conn, put_conn
from qwenpaw.platform_store import paths as P

from tests.unit.platform_store.test_legacy_import import (  # noqa: E402
    requires_platform_db,
)


class _Req:
    def __init__(self, user: str | None) -> None:
        self.state = type("S", (), {"user": user})()
        self.headers: dict[str, str] = {}


@pytest.fixture()
def account():
    """admin 账号（有真实智能体）+ 结束后**恢复**原授权并清理新建路径。

    Restoring rather than deleting matters: these tests rewrite a real agent's
    grants, and a blind ``DELETE FROM agent_path_bindings`` would also drop the
    root grant the agent legitimately had before the test — quietly narrowing a
    live agent's access as a side effect of running the suite.
    """
    created: list[str] = []
    # agent_id -> the exact grant set to put back afterwards.
    restore: dict[str, list[str]] = {}
    yield "admin", created, restore
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for agent_id, path_ids in restore.items():
                cur.execute(
                    'DELETE FROM "platform".agent_path_bindings '
                    "WHERE agent_id = %s",
                    (agent_id,),
                )
                for path_id in path_ids:
                    cur.execute(
                        'INSERT INTO "platform".agent_path_bindings '
                        "(agent_id, path_id) VALUES (%s, %s) "
                        "ON CONFLICT (agent_id, path_id) DO NOTHING",
                        (agent_id, path_id),
                    )
            for path_id in created:
                cur.execute(
                    'DELETE FROM "platform".paths WHERE id = %s', (path_id,),
                )
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
    finally:
        put_conn(conn)


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _make_path(req, created, name):
    result = _run(api.create_path(req, api.PathCreateRequest(name=name)))
    created.append(result["id"])
    return result["id"]


def _owned_agent(req, restore) -> str:
    """Pick an owned agent and remember its current grants for teardown."""
    grants = _run(api.list_grants(req))["grants"]
    if not grants:
        pytest.skip("account owns no agents")
    agent_id = next(iter(grants))
    restore.setdefault(agent_id, list(grants[agent_id]))
    return agent_id


@requires_platform_db
def test_setting_grants_replaces_the_whole_set(account):
    """整体替换：勾选结果就是智能体的实际可达集合。

    逐条增删时，两次请求之间会存在「界面已变、服务端未变」的窗口；整体替换
    消除这个窗口，也让「取消勾选某条路径」有确定的语义。
    """
    req = _Req(account[0])
    created, restore = account[1], account[2]
    agent_id = _owned_agent(req, restore)

    path_a = _make_path(req, created, f"A-{uuid.uuid4().hex[:6]}")
    path_b = _make_path(req, created, f"B-{uuid.uuid4().hex[:6]}")

    # Grant both, then narrow to one: the dropped path must actually be gone.
    _run(api.set_agent_grants(
        req, api.AgentPathsRequest(agent_id=agent_id, path_ids=[path_a, path_b]),
    ))
    assert set(_run(api.get_agent_grants(req, agent_id))["path_ids"]) == {path_a, path_b}

    _run(api.set_agent_grants(
        req, api.AgentPathsRequest(agent_id=agent_id, path_ids=[path_a]),
    ))
    assert _run(api.get_agent_grants(req, agent_id))["path_ids"] == [path_a]

    # An empty set is meaningful: the agent reads nothing (fail-closed).
    _run(api.set_agent_grants(
        req, api.AgentPathsRequest(agent_id=agent_id, path_ids=[]),
    ))
    assert _run(api.get_agent_grants(req, agent_id))["path_ids"] == []


@requires_platform_db
def test_grants_cannot_be_written_for_another_accounts_agent(account):
    """不能给不属于本账号的智能体授权。

    否则可以把他人的智能体接到自己的案件路径上 —— 这是聊天页可达的入口，
    必须在这里挡住，而不是只靠前端隐藏。
    """
    req = _Req(account[0])
    created = account[1]
    path_id = _make_path(req, created, f"越权-{uuid.uuid4().hex[:6]}")

    with pytest.raises(HTTPException) as excinfo:
        _run(api.set_agent_grants(
            req,
            api.AgentPathsRequest(
                agent_id=f"not-mine-{uuid.uuid4().hex[:6]}", path_ids=[path_id],
            ),
        ))
    assert excinfo.value.status_code == 403

    with pytest.raises(HTTPException) as read_exc:
        _run(api.get_agent_grants(req, f"not-mine-{uuid.uuid4().hex[:6]}"))
    assert read_exc.value.status_code == 403


@requires_platform_db
def test_granting_a_foreign_path_is_rejected(account):
    """不能把不属于本账号的路径授给自己的智能体（否则等于读别人的案件）。

    用一个确实不存在/不属于本账号的路径 id：归属校验必须把它判 404，并且
    **不留下部分写入** —— 失败后原有授权要保持不变，否则一次失败的勾选就可能
    悄悄改变智能体的可达范围。
    """
    req = _Req(account[0])
    restore = account[2]
    agent_id = _owned_agent(req, restore)
    before = _run(api.get_agent_grants(req, agent_id))["path_ids"]

    unknown = str(uuid.uuid4())
    with pytest.raises(HTTPException) as excinfo:
        _run(api.set_agent_grants(
            req,
            api.AgentPathsRequest(agent_id=agent_id, path_ids=[unknown]),
        ))
    assert excinfo.value.status_code == 404

    after = _run(api.get_agent_grants(req, agent_id))["path_ids"]
    assert after == before, "失败的写入不得改变已有授权"
    assert unknown not in after
    assert P.agent_path_ids(agent_id) == before


@requires_platform_db
def test_empty_selection_is_fail_closed(account, monkeypatch):
    """没有任何路径授权时，智能体读不到东西（不是「全部可见」）。

    这是隔离的基础方向：空集合必须是拒绝，不能退化成不过滤。这里不只看
    「授权表为空」，而是走一遍智能体侧的判定入口 —— 界面把路径全部取消勾选后，
    智能体必须真的什么都读不到。
    """
    req = _Req(account[0])
    restore = account[2]
    agent_id = _owned_agent(req, restore)

    _run(api.set_agent_grants(
        req, api.AgentPathsRequest(agent_id=agent_id, path_ids=[]),
    ))
    assert P.agent_path_ids(agent_id) == []

    # Every enforcement point resolves "who is asking" through this function.
    from qwenpaw.agents.tools import path_scope

    monkeypatch.setattr(path_scope, "current_agent_id", lambda: agent_id)
    assert path_scope.visible_document_ids(account[0], ["doc-1"]) == []
    assert path_scope.data_search_path(account[0]) is None
