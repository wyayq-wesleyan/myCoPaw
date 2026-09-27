# -*- coding: utf-8 -*-
"""数据表按路径过滤（list_tables 的 path_id 参数）。

这里覆盖两个实测踩到的坑，都属于「不报错但结果错」的类型：

1. FastAPI 未参与调用时，``Query(...)`` 默认值会作为真实值传进函数体 —— 进 SQL
   会适配失败，算术会 TypeError，而失败被外层包成 503，看起来像存储故障而不是
   调用方式问题。
2. 注册表主键是 ``uuid``，路径绑定存的是 ``text``，直接比较会被 PostgreSQL 拒绝
   （operator does not exist: uuid = text）。
"""

from __future__ import annotations

import uuid

import pytest

from qwenpaw.data_tables import table_api
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
def owner_and_table() -> tuple[str, list[str]]:
    """``(account, cleanup-list)``.

    The list collects ``[path_id, table_id]`` pairs created by a test; teardown
    removes the binding and the path so a failed assertion cannot leave the
    account with a stray case folder.
    """
    owner = "admin"
    created: list[str] = []
    yield owner, created
    if created:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                if len(created) > 1:
                    cur.execute(
                        'DELETE FROM "platform".resource_bindings '
                        "WHERE resource_id = %s",
                        (created[1],),
                    )
                cur.execute(
                    'DELETE FROM "platform".paths WHERE id = %s',
                    (created[0],),
                )
            conn.commit()
        finally:
            put_conn(conn)


@requires_platform_db
def test_list_tables_tolerates_fastapi_query_defaults(owner_and_table):
    """直接用 ``Query(...)`` 默认值调用不能炸。

    端点函数会被内部调用（测试、其它路由），此时参数是 ``Query`` 对象而不是真实
    值。不做归一化的话，一个普通列表请求会以 503「路径解析失败」告终 —— 症状
    指向数据库，根因却在调用约定上，很难排查。
    """
    owner, _created = owner_and_table
    req = _Req(owner)
    # 不传任何参数：全部使用函数签名里的 Query 默认值。
    result = table_api.list_tables(req)
    assert isinstance(result.get("total"), int)


@requires_platform_db
def test_path_filter_matches_uuids_stored_as_text(owner_and_table):
    """注册表 uuid 与绑定表 text 的 id 必须能匹配上。

    ``resource_bindings.resource_id`` 是 text（它也存知识空间的字符串 id），而
    ``tb_hzxzh_data_table_registry.id`` 是 uuid。不转换类型时 PostgreSQL 直接
    拒绝查询 —— 表现为过滤功能完全不可用。
    """
    owner, created = owner_and_table
    req = _Req(owner)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM tb_hzxzh_data_table_registry "
                "WHERE owner_username = %s LIMIT 1",
                (owner,),
            )
            row = cur.fetchone()
    finally:
        put_conn(conn)
    if not row:
        pytest.skip("no data table available to file under a path")
    table_id = str(row[0])

    path_id = P.create_path(owner, f"筛选-{uuid.uuid4().hex[:6]}")
    created.append(path_id)
    created.append(table_id)
    P.bind_resource(path_id, P.KIND_DATA_TABLE, table_id)

    filtered = table_api.list_tables(req, path_id=path_id)
    assert filtered["total"] == 1
    assert filtered["items"][0]["id"] == table_id

    # The public area keeps everything else: the two must partition the account.
    unfiltered = table_api.list_tables(req)
    root = P.ensure_root_path(owner)
    public = table_api.list_tables(req, path_id=root)
    assert filtered["total"] + public["total"] == unfiltered["total"]


@requires_platform_db
def test_unknown_path_returns_empty_not_everything(owner_and_table):
    """未知路径必须返回空页，不能退化为不过滤。

    退化会把其它路径的表泄漏进当前视图，正好破坏这次改造要建立的隔离。
    """
    owner, _created = owner_and_table
    req = _Req(owner)
    result = table_api.list_tables(req, path_id=str(uuid.uuid4()))
    assert result["total"] == 0
    assert result["items"] == []


@requires_platform_db
def test_path_of_another_account_is_not_visible(owner_and_table):
    """别人账号的路径同样返回空页，而不是报错泄漏其存在性。"""
    owner, _created = owner_and_table
    stranger = f"filterother-{uuid.uuid4().hex[:8]}"
    foreign = P.ensure_root_path(stranger, "默认")
    try:
        req = _Req(owner)
        result = table_api.list_tables(req, path_id=foreign)
        assert result["total"] == 0
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
