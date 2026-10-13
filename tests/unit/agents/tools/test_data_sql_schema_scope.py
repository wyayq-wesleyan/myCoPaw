# -*- coding: utf-8 -*-
"""数据表 SQL 工具的 schema 越界防护。

``search_path`` 只决定**未限定名**怎么解析，它本身不是权限边界：任何查询都可以直接
写出另一个 schema 名 ——

    SELECT * FROM p_<另一个案件>."tb_secret"

这条语句完全合法，因为名字已经限定好了，``search_path`` 不起作用。而
``query_data_table`` 执行的是模型写的 SQL，所以这是一个真实的越权通道：实测一个
只被挂载「案件A」的智能体能读到案件B的表（并返回了对方的行数），还能通过
``pg_namespace`` 把 schema 名列出来去找。

这一层就是用来堵住它的：只要查询里出现了该智能体未持有的路径 schema 名，直接拒绝。
schema 名由 UUID 推导，正常查询没有任何理由引用别人的。
"""

from __future__ import annotations

import uuid

import pytest

from qwenpaw.agents.tools.data_tables import (
    SchemaScopeError,
    _enforce_schema_scope,
)
from qwenpaw.platform_store import paths as P

from ...platform_store.test_legacy_import import (  # noqa: E402
    requires_platform_db,
)

# A stable pair of schema names for the pure-function cases.
OWN = "p_11111111111111111111111111111111"
OTHER = "p_22222222222222222222222222222222"
KNOWN = ["public", OWN, OTHER]


def _blocked(sql: str, allowed: list[str] | None = None) -> bool:
    try:
        _enforce_schema_scope(sql, allowed or [OWN], KNOWN)
        return False
    except SchemaScopeError:
        return True


class TestSchemaScopeGuard:
    def test_allows_unqualified_own_table(self):
        """未限定名由 search_path 解析，属于正常用法。"""
        assert not _blocked('SELECT count(*) FROM "tb_mine"')

    def test_allows_explicitly_naming_the_own_schema(self):
        """限定到**自己**的 schema 是合法的（JOIN 自身两表时常用）。"""
        assert not _blocked(f'SELECT * FROM "{OWN}"."tb_mine"')

    def test_blocks_naming_another_cases_schema(self):
        """核心断言：引用别的路径 schema 一律拒绝。"""
        assert _blocked(f'SELECT count(*) FROM "{OTHER}"."tb_secret"')

    def test_blocks_unquoted_foreign_schema(self):
        """不加引号一样拦得住（标识符 token 也参与匹配）。"""
        assert _blocked(f'SELECT * FROM {OTHER}."tb_secret"')

    def test_blocks_foreign_schema_used_in_where(self):
        """即使只出现在 WHERE 里也要拦：值比较同样能推断对方数据。"""
        assert _blocked(f"SELECT 1 FROM t WHERE x = {OTHER}")

    def test_blocks_public_when_root_is_not_granted(self):
        """``public`` 是根路径的 schema，没被授予根就不该能引用它。"""
        assert _blocked('SELECT * FROM public.some_registry')

    def test_allows_public_when_root_is_granted(self):
        """授予了根路径时，引用 public 是正常的。"""
        assert not _blocked('SELECT * FROM public.t', allowed=["public", OWN])

    def test_still_blocks_other_case_when_root_is_granted(self):
        """授了根也不能顺手读到别的案件。"""
        assert _blocked(f'SELECT * FROM "{OTHER}"."t"', allowed=["public", OWN])

    def test_string_literal_naming_a_schema_is_not_treated_as_a_reference(self):
        """字符串字面量里的 schema 名不算引用。

        否则一个数据值恰好长得像 schema 名就会把查询误杀；实测里文件名叫
        ``p_xxx.csv`` 这类数据是存在的。
        """
        assert not _blocked(f"SELECT '{OTHER}' AS label")
        assert not _blocked("SELECT * FROM t WHERE filename LIKE 'p_%.csv'")

    def test_allows_system_catalogs(self):
        """系统目录不在拦截范围内（模型查类型/列信息会用到）。"""
        assert not _blocked("SELECT count(*) FROM pg_catalog.pg_class")

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT pg_read_file('/etc/passwd')",
            "SELECT pg_read_binary_file('/etc/passwd')",
            "SELECT pg_ls_dir('/')",
            "SELECT lo_import('/etc/passwd')",
        ],
    )
    def test_blocks_server_file_access(self, sql):
        """读服务器文件的函数与业务无关，直接拒绝。"""
        assert _blocked(sql)


@pytest.fixture()
def two_cases():
    """两个路径 + 两个 agent，结束后清理。"""
    owner = "admin"
    suffix = uuid.uuid4().hex[:6]
    case_a = P.create_path(owner, f"sqlguard-A-{suffix}")
    case_b = P.create_path(owner, f"sqlguard-B-{suffix}")
    yield owner, case_a, case_b
    from qwenpaw.platform_store import get_conn, put_conn

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                'DELETE FROM "platform".paths WHERE owner_username = %s AND name LIKE %s',
                (owner, f"sqlguard-%"),
            )
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
    finally:
        put_conn(conn)


@requires_platform_db
def test_known_path_schemas_lists_real_schemas(two_cases):
    """``_known_path_schemas`` 必须真的列出库里的路径 schema。

    它为空时防护会**静默失效**（没有任何名字被判为「已知 schema」，于是什么都不拦）
    —— 这正是它第一次实现时的 bug（``psycopg2`` 未导入，异常被吞掉）。所以这里
    断言它非空且包含刚建的路径。
    """
    from qwenpaw.data_tables.store import ensure_path_schema
    from qwenpaw.agents.tools.data_tables import _known_path_schemas

    owner, case_a, _case_b = two_cases
    ensure_path_schema(owner, P.data_schema_for(case_a))

    schemas = _known_path_schemas(owner)

    assert schemas, "防护依赖的 schema 列表不能为空（为空即等于不防护）"
    assert P.data_schema_for(case_a) in schemas
    assert "public" in schemas
