"""The table-definition core is shared by the console API and the agent tool.

The agent path deliberately allows an empty column list: the chat user often
cannot describe their columns, and the importer infers real names and types
from the file header when the table is created on first import.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi import HTTPException

# psycopg2 ships in the base deploy image (deploy/Dockerfile.base), not in the
# Python project metadata, so these tests only run where it is installed.
pytest.importorskip("psycopg2")

from qwenpaw.data_tables import table_api  # noqa: E402
from tests.unit.platform_store.test_legacy_import import (  # noqa: E402
    requires_platform_db,
)


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append((" ".join(str(sql).split()), params))

    def fetchone(self):
        return self._conn.results.pop(0) if self._conn.results else None


class _FakeConn:
    def __init__(self, results):
        self.results = list(results)
        self.executed: list[tuple[str, object]] = []
        self.committed = False

    def cursor(self, *args, **kwargs):
        return _FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


@pytest.fixture
def table_api_env(monkeypatch):
    """Wire the table core to a fake connection with scripted results."""
    conn = _FakeConn(results=[])

    monkeypatch.setattr(table_api, "_get_conn", lambda: conn)
    monkeypatch.setattr(table_api, "_put_conn", lambda _conn: None)
    monkeypatch.setattr(table_api, "ensure_user_database", lambda username: f"db_{username}")
    monkeypatch.setattr(table_api, "generate_table_name", lambda _a, _b: "tb_hzxzs_test_0001")
    return conn


def _insert_params(conn: _FakeConn) -> tuple:
    for sql, params in conn.executed:
        if sql.startswith("INSERT INTO"):
            return params
    raise AssertionError("no INSERT was executed")


def test_create_table_record_accepts_no_columns(table_api_env):
    """The agent path creates a table before columns are known."""
    table_api_env.results = [None, (7,)]

    record = table_api.create_table_record("u1", description="2024 年销售明细")

    assert record == {"id": "7", "table_name": "tb_hzxzs_test_0001"}
    # ``path_id`` is the last bound parameter now; columns are second to last.
    assert _insert_params(table_api_env)[-2] == json.dumps([])
    # A caller that names no path registers the table in the account's public
    # (root) area, which is what keeps the pre-path behaviour unchanged.
    assert _insert_params(table_api_env)[-1] == ""
    assert table_api_env.committed is True


def test_create_table_record_trims_and_drops_blank_column_notes(table_api_env):
    table_api_env.results = [None, (8,)]

    table_api.create_table_record(
        "u1",
        display_name="销售",
        description="销售明细",
        columns=["  订单号  ", "   ", "金额"],
    )

    notes = json.loads(_insert_params(table_api_env)[-2])
    assert [note["comment"] for note in notes] == ["订单号", "金额"]
    assert all(note["column_name"] is None for note in notes)


@requires_platform_db
def test_create_table_record_writes_the_authoritative_binding(table_api_env):
    """点名路径时必须写入**权威的绑定记录**，而不只是注册表的镜像列。

    成员关系的唯一事实来源是 ``platform.resource_bindings``：列表过滤、物理表
    所在的 schema、以及查询工具的 search_path 都由它推导。只写注册表的 ``path_id``
    列会造成「逻辑上属于案件、物理上仍在公共区」，案件智能体看不到系统刚展示给
    它的那张表 —— 正是本功能要消除的隔离失效。

    这里用真实路径存储（而非替身）验证：替身只能证明传参正确，证明不了绑定真的
    能在联表查询里被读到。
    """
    from qwenpaw.platform_store import get_conn, put_conn
    from qwenpaw.platform_store import paths as path_store

    owner = f"tblpath-{uuid.uuid4().hex[:8]}"
    path_id = path_store.ensure_root_path(owner, "默认")
    case = path_store.create_path(owner, "案件一")
    try:
        table_api_env.results = [None, (11,)]
        table_api.create_table_record(
            "u1", description="案件一材料", path_id=case,
        )
        # 权威记录已建立，且指向案件而非根。
        assert path_store.path_of_resource(
            path_store.KIND_DATA_TABLE, "11",
        ) == case
    finally:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    'DELETE FROM "platform".resource_bindings '
                    "WHERE resource_id = %s",
                    ("11",),
                )
                cur.execute(
                    'DELETE FROM "platform".paths WHERE owner_username = %s',
                    (owner,),
                )
            conn.commit()
        finally:
            put_conn(conn)


def test_create_table_record_without_path_writes_no_binding(table_api_env):
    """不指定路径时不写绑定 —— 未绑定即公共区，这是升级零搬迁的基础。

    用真实的路径存储断言「没有产生任何绑定」：若这里写了一条指向根路径的绑定，
    语义就从「未归类」变成「明确归档到公共」，将来把根路径内容重新归类时行为会
    不一致。
    """
    from qwenpaw.platform_store import paths as path_store

    table_api_env.results = [None, (12,)]
    table_api.create_table_record("u1", description="公共资料")

    assert path_store.path_of_resource(
        path_store.KIND_DATA_TABLE, "12",
    ) is None
