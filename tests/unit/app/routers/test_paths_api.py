# -*- coding: utf-8 -*-
"""路径管理 API 测试。

重点是**归属校验**：这些端点暴露「谁的资源挂在哪条路径、哪些智能体能看到」，
一次漏检就等于把别人的案件结构暴露给对方。因此每个端点都要在账号边界上验证，
而不只是验证"功能能用"。

断言不依赖 HTTP 鉴权中间件（测试环境不启用），改为直接构造带身份的请求对象调用
端点函数 —— 这样既覆盖了端点逻辑，又不必搭一套鉴权环境。
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
    """Minimal stand-in for a FastAPI Request carrying an identity."""

    def __init__(self, user: str | None) -> None:
        self.state = type("S", (), {"user": user})()
        self.headers: dict[str, str] = {}


@pytest.fixture()
def account() -> str:
    """The real admin account (has agents), cleaned of test paths afterwards."""
    name = "admin"
    created: list[str] = []
    yield_name = name, created
    yield yield_name
    if created:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    'DELETE FROM "platform".paths WHERE id = ANY(%s)',
                    (created,),
                )
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
        finally:
            put_conn(conn)


def _make_path(req: _Req, created: list[str], name: str) -> str:
    import asyncio

    result = asyncio.run(
        api.create_path(req, api.PathCreateRequest(name=name)),
    )
    created.append(result["id"])
    return result["id"]


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# --- 身份 -------------------------------------------------------------------


def test_missing_identity_is_rejected_when_auth_is_enabled(monkeypatch):
    """鉴权启用时，身份不明必须 401，而不是退化成某个账号。

    退化到 admin 尤其危险：任何身份解析失败的请求都会拿到管理员的路径树。
    """
    monkeypatch.setattr(api, "_auth_enabled", lambda: True)
    req = _Req(None)
    with pytest.raises(HTTPException) as excinfo:
        _run(api.list_paths(req))
    assert excinfo.value.status_code == 401


@requires_platform_db
def test_missing_identity_uses_the_single_account_when_auth_is_disabled():
    """鉴权未启用时（单账号部署）不返回 401，而是用本部署自己的账号。

    返回 401 在这里比无用更糟：前端的 401 拦截器会清空会话并跳转登录页，而这类
    部署根本没有登录页 —— 于是路径树一加载就把用户踢出应用。这正是浏览器实测
    发现的问题。用具体账号名（而非空串）是必需的：路径按 owner 存储，空的 owner
    只会得到空树，表现为功能坏掉。
    """
    req = _Req(None)
    result = _run(api.list_paths(req))
    assert "tree" in result
    # 必须解析到一个真实存在的根路径，而不是空树。
    assert result["tree"], "单账号部署下应能看到该账号的根路径"


# --- 树 ---------------------------------------------------------------------


@requires_platform_db
def test_create_and_list_tree(account):
    req = _Req(account[0])
    created = account[1]
    name = f"API案件-{uuid.uuid4().hex[:6]}"
    path_id = _make_path(req, created, name)

    tree = _run(api.list_paths(req))["tree"]
    # The account's root is present, and the new child hangs off it.
    root = next(item for item in tree if item["is_root"])
    assert path_id in {child["id"] for child in root["children"]}


@requires_platform_db
def test_duplicate_sibling_name_is_a_client_error(account):
    """重名返回 400（可读原因），而不是 500。"""
    req = _Req(account[0])
    created = account[1]
    name = f"API重名-{uuid.uuid4().hex[:6]}"
    _make_path(req, created, name)

    with pytest.raises(HTTPException) as excinfo:
        _run(api.create_path(req, api.PathCreateRequest(name=name)))
    assert excinfo.value.status_code == 400
    assert "已存在" in excinfo.value.detail


@requires_platform_db
def test_renaming_and_moving(account):
    req = _Req(account[0])
    created = account[1]
    tag = uuid.uuid4().hex[:6]
    path_id = _make_path(req, created, f"移动前-{tag}")
    other = _make_path(req, created, f"目标-{tag}")

    _run(api.rename_path(req, path_id, api.PathRenameRequest(name=f"移动后-{tag}")))
    assert P.path_by_id(path_id)["name"] == f"移动后-{tag}"

    _run(api.move_path(req, path_id, api.PathMoveRequest(parent_id=other)))
    assert P.path_by_id(path_id)["parent_id"] == other


@requires_platform_db
def test_cannot_move_a_path_into_its_own_subtree(account):
    """环形移动必须被拒绝（拖拽 UI 不能保证这一点）。"""
    req = _Req(account[0])
    created = account[1]
    tag = uuid.uuid4().hex[:6]
    parent = _make_path(req, created, f"父-{tag}")
    child = _make_path(req, created, f"子-{tag}")
    _run(api.move_path(req, child, api.PathMoveRequest(parent_id=parent)))

    with pytest.raises(HTTPException) as excinfo:
        _run(api.move_path(req, parent, api.PathMoveRequest(parent_id=child)))
    assert excinfo.value.status_code == 400


@requires_platform_db
def test_unknown_path_is_404(account):
    req = _Req(account[0])
    with pytest.raises(HTTPException) as excinfo:
        _run(api.list_path_resources(req, str(uuid.uuid4())))
    assert excinfo.value.status_code == 404


@requires_platform_db
def test_another_accounts_path_looks_missing(account):
    """别人的路径必须表现为「不存在」，而不是 403。

    区分「不存在」与「无权访问」会确认对方路径的存在性，等于泄漏了另一账号
    的结构信息。
    """
    stranger = f"pathsapi-other-{uuid.uuid4().hex[:8]}"
    other_root = P.ensure_root_path(stranger, "默认")
    foreign = P.create_path(stranger, "对方案件", parent_id=other_root)
    try:
        req = _Req(account[0])
        with pytest.raises(HTTPException) as excinfo:
            _run(api.list_path_resources(req, foreign))
        assert excinfo.value.status_code == 404
    finally:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    'DELETE FROM "platform".paths WHERE owner_username = %s',
                    (stranger,),
                )
            conn.commit()
        finally:
            put_conn(conn)


# --- 删除保护 ---------------------------------------------------------------


@requires_platform_db
def test_delete_refuses_when_resources_are_attached(account):
    req = _Req(account[0])
    created = account[1]
    path_id = _make_path(req, created, f"有资源-{uuid.uuid4().hex[:6]}")
    _run(
        api.bind_resource(
            req, path_id,
            api.BindResourceRequest(
                resource_kind=P.KIND_KNOWLEDGE_SPACE, resource_id="space-x",
            ),
        ),
    )

    with pytest.raises(HTTPException) as excinfo:
        _run(api.delete_path(req, path_id))
    assert excinfo.value.status_code == 400
    assert "还有内容" in excinfo.value.detail

    _run(api.unbind_resource(req, path_id, P.KIND_KNOWLEDGE_SPACE, "space-x"))


@requires_platform_db
def test_delete_refuses_while_an_agent_is_attached(account):
    """仍挂着智能体的路径不算空 —— 删除会静默撤销它的访问权。"""
    req = _Req(account[0])
    created = account[1]
    path_id = _make_path(req, created, f"有智能体-{uuid.uuid4().hex[:6]}")
    agents = _run(api.list_grants(req))["grants"]
    agent_id = next(iter(agents))

    _run(api.bind_agent(req, path_id, api.BindAgentRequest(agent_id=agent_id)))
    with pytest.raises(HTTPException) as excinfo:
        _run(api.delete_path(req, path_id))
    assert excinfo.value.status_code == 400

    _run(api.unbind_agent(req, path_id, agent_id))
    assert _run(api.delete_path(req, path_id))["deleted"] is True


# --- 资源与智能体绑定 -------------------------------------------------------


@requires_platform_db
def test_bind_and_unbind_resource(account):
    req = _Req(account[0])
    created = account[1]
    path_id = _make_path(req, created, f"资源-{uuid.uuid4().hex[:6]}")
    resource_id = f"resource-{uuid.uuid4().hex[:6]}"

    _run(
        api.bind_resource(
            req, path_id,
            api.BindResourceRequest(
                resource_kind=P.KIND_DATA_TABLE, resource_id=resource_id,
            ),
        ),
    )
    listed = _run(api.list_path_resources(req, path_id))["resources"]
    assert {"resource_kind": P.KIND_DATA_TABLE, "resource_id": resource_id} in listed

    _run(api.unbind_resource(req, path_id, P.KIND_DATA_TABLE, resource_id))
    assert _run(api.list_path_resources(req, path_id))["resources"] == []


@requires_platform_db
def test_agent_grant_requires_an_owned_agent(account):
    """只能把路径挂到自己账号的智能体上。

    否则可以给别人的智能体授权，或把路径挂到一个不属于本账号的 id 上制造混乱。
    """
    req = _Req(account[0])
    created = account[1]
    path_id = _make_path(req, created, f"归属-{uuid.uuid4().hex[:6]}")

    with pytest.raises(HTTPException) as excinfo:
        _run(
            api.bind_agent(
                req, path_id,
                api.BindAgentRequest(agent_id=f"not-mine-{uuid.uuid4().hex[:6]}"),
            ),
        )
    assert excinfo.value.status_code == 403


@requires_platform_db
def test_grants_view_lists_paths_per_agent(account):
    """``/grants`` 给出「每个智能体能看哪些路径」的反向视图。"""
    req = _Req(account[0])
    created = account[1]
    path_id = _make_path(req, created, f"授权视图-{uuid.uuid4().hex[:6]}")
    agent_id = next(iter(_run(api.list_grants(req))["grants"]))

    _run(api.bind_agent(req, path_id, api.BindAgentRequest(agent_id=agent_id)))
    grants = _run(api.list_grants(req))["grants"]
    assert path_id in grants[agent_id]

    _run(api.unbind_agent(req, path_id, agent_id))


@requires_platform_db
def test_path_agents_only_lists_this_accounts_agents(account):
    """路径下的智能体列表要按归属过滤，避免显示已转走的智能体。"""
    req = _Req(account[0])
    created = account[1]
    path_id = _make_path(req, created, f"列表-{uuid.uuid4().hex[:6]}")
    agent_id = next(iter(_run(api.list_grants(req))["grants"]))

    _run(api.bind_agent(req, path_id, api.BindAgentRequest(agent_id=agent_id)))
    listed = _run(api.list_path_agents(req, path_id))["agents"]
    assert listed == [agent_id]

    _run(api.unbind_agent(req, path_id, agent_id))
