# -*- coding: utf-8 -*-
"""每个账号都必须拥有公共空间（根路径）。

**为什么这是一条不变量。** 路径树是知识空间与数据空间页的**唯一入口**：没有根路径，
两页的左侧都是空的，用户看不到任何东西、也无从新建子路径。而路径判定是
fail-closed 的，所以新账号若没有根路径，它的智能体连「共享材料」都读不到 ——
用户看到的是「产品坏了」，而不是「我还没挂载路径」。

实测报上来的现象就是新建账号 `{"tree":[]}`：根路径只在**启动迁移**里创建，而启动
迁移只遍历「已有智能体的账号」且每个 boot 只跑一次。于是运行期间新建的账号（或
智能体档案后来才出现的账号）要等到下次重启才有树。

这三处现在都保证它成立，缺一处就会出现「某些账号没树」：
1. 建账号时立即创建（`create_user_account`）；
2. 账号的默认智能体创建时授予公共区（`ensure_user_default_agent`）；
3. 读路径树时自愈（`GET /paths`、`GET /paths/tree`）——最后一道保险；
4. 启动迁移覆盖**没有智能体**的账号。
"""

from __future__ import annotations

import uuid

import pytest

from qwenpaw.app.routers import paths as paths_api
from qwenpaw.platform_store import get_conn, put_conn
from qwenpaw.platform_store import paths as P
from qwenpaw.platform_store import path_migration

from tests.unit.platform_store.test_legacy_import import (  # noqa: E402
    requires_platform_db,
)


class _Req:
    """带身份的最小 Request 替身（与 paths API 测试同一约定）。"""

    def __init__(self, user: str) -> None:
        self.state = type("S", (), {"user": user})()
        self.headers: dict[str, str] = {}


@pytest.fixture()
def account():
    """一个临时账号名，结束后清掉它的一切。"""
    name = f"treeprobe-{uuid.uuid4().hex[:10]}"
    yield name
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                'DELETE FROM "platform".paths WHERE owner_username = %s', (name,),
            )
            cur.execute(
                'DELETE FROM "platform".agent_path_bindings WHERE agent_id LIKE %s',
                (f"%{name}%",),
            )
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
    finally:
        put_conn(conn)


def _run(coro):
    import asyncio

    return asyncio.run(coro)


class TestTreeEndpointSelfHeals:
    @requires_platform_db
    def test_list_paths_creates_the_root_when_missing(self, account):
        """账号没有根路径时，读列表就补上。

        这是「新账号树是空的」的直接修复：不依赖重启，用户打开页面即修好。
        """
        assert P.list_path_tree(account) == []

        result = _run(paths_api.list_paths(_Req(account)))

        assert len(result["tree"]) == 1
        assert result["tree"][0]["is_root"] is True
        # And it must be the *same* root the rest of the system uses.
        assert result["tree"][0]["id"] == P.ensure_root_path(account)

    @requires_platform_db
    def test_tree_endpoint_creates_the_root_when_missing(self, account):
        """``/paths/tree`` 同样自愈（知识空间与数据空间页用的是这个）。"""
        assert P.list_path_tree(account) == []

        result = _run(paths_api.path_tree_with_contents(_Req(account)))

        assert len(result["tree"]) == 1
        assert result["tree"][0]["is_root"] is True

    @requires_platform_db
    def test_self_heal_does_not_duplicate_an_existing_root(self, account):
        """已有根路径时不能重复创建（唯一索引与幂等都要成立）。"""
        first = P.ensure_root_path(account)

        result = _run(paths_api.list_paths(_Req(account)))

        assert len(result["tree"]) == 1
        assert result["tree"][0]["id"] == first

    @requires_platform_db
    def test_self_heal_does_not_grant_anything(self, account):
        """自愈只建路径，**不授予**任何智能体。

        用户可能在对话界面主动取消了公共区挂载（拒绝该智能体读公共材料）；
        读一次页面又把授权加回去，就等于悄悄撤销了用户的选择。
        """
        _run(paths_api.list_paths(_Req(account)))
        root = P.ensure_root_path(account)

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    'SELECT count(*) FROM "platform".agent_path_bindings '
                    "WHERE path_id = %s",
                    (root,),
                )
                grants = cur.fetchone()[0]
        finally:
            put_conn(conn)
        assert grants == 0, "自愈不得替用户授予任何路径"


class TestMigrationCoversAgentlessAccounts:
    @requires_platform_db
    def test_migration_enumerates_accounts_not_only_agents(self, monkeypatch):
        """迁移的账号来源必须包含「没有智能体的账号」。

        这条用打桩把两边的输入都钉死，直接验证合并逻辑，避免依赖真实账号。
        """
        monkeypatch.setattr(
            path_migration, "_iter_account_agents",
            lambda: {"acct-with-agent": ["ag-1"]},
        )
        monkeypatch.setattr(
            path_migration, "_iter_accounts",
            lambda: ["acct-with-agent", "acct-without-agent"],
        )
        seen: list[str] = []
        monkeypatch.setattr(
            path_migration.path_store, "ensure_root_path",
            lambda owner, *a, **k: seen.append(owner) or f"root-{owner}",
        )
        monkeypatch.setattr(
            path_migration.path_store, "grant_root_to_agent",
            lambda owner, agent: f"root-{owner}",
        )

        report = path_migration.migrate_into_path_scoping()

        assert "acct-without-agent" in seen, "没有智能体的账号被漏掉了"
        assert report.accounts == 2
        assert not report.errors


class TestRootPathIsSingular:
    @requires_platform_db
    def test_only_one_root_per_account(self, account):
        """一个账号只能有一个根路径（部分唯一索引保证）。"""
        first = P.ensure_root_path(account)
        second = P.ensure_root_path(account)
        assert first == second

        roots = [n for n in P.list_path_tree(account) if n.is_root]
        assert len(roots) == 1

    @requires_platform_db
    def test_root_is_created_with_a_readable_name(self, account):
        """根路径要有名字，因为界面上「公共区（未归类）」的判定依赖 is_root，
        而原始名字会出现在别处（如转移弹窗）。"""
        root_id = P.ensure_root_path(account)
        node = P.path_by_id(root_id)
        assert node["is_root"] is True
        assert node["name"]
        assert node["parent_id"] is None


class TestAccountDeletionClearsItsTree:
    @requires_platform_db
    def test_deleting_an_account_removes_its_whole_tree(self, account):
        """删除账号要连带清掉它的路径树（含嵌套与绑定）。

        路径是按 owner 存的，账号删掉后这些行会永久残留；而 ``*_default`` 这类智能体
        id 由账号名推导，同名账号重建时可能继承旧树的授权。绑定行还带外键，不清掉
        会让路径无法删除。实测：删除账号后路径与授权仍在。
        """
        from qwenpaw.app.user_space import _delete_account_paths

        root = P.ensure_root_path(account)
        child = P.create_path(account, "案件一", parent_id=root)
        grand = P.create_path(account, "上证材料", parent_id=child)
        P.bind_resource(child, P.KIND_KNOWLEDGE_DOCUMENT, f"{account}-doc")
        P.bind_agent(f"{account}-ag", grand)
        assert len(P.list_path_tree(account)) == 1  # one tree (root + children)

        _delete_account_paths(account)

        assert P.list_path_tree(account) == []
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    'SELECT count(*) FROM "platform".resource_bindings '
                    "WHERE resource_id = %s",
                    (f"{account}-doc",),
                )
                assert cur.fetchone()[0] == 0
                cur.execute(
                    'SELECT count(*) FROM "platform".agent_path_bindings '
                    "WHERE agent_id = %s",
                    (f"{account}-ag",),
                )
                assert cur.fetchone()[0] == 0
        finally:
            put_conn(conn)

    @requires_platform_db
    def test_deleting_one_account_leaves_others_untouched(self, account):
        """删除一个账号不能影响别的账号的树。"""
        from qwenpaw.app.user_space import _delete_account_paths

        other = f"{account}-other"
        P.ensure_root_path(account)
        other_root = P.ensure_root_path(other)
        try:
            _delete_account_paths(account)
            assert P.list_path_tree(account) == []
            assert [
                n.id for n in P.list_path_tree(other) if n.is_root
            ] == [other_root]
        finally:
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        'DELETE FROM "platform".paths WHERE owner_username = %s',
                        (other,),
                    )
                conn.commit()
            finally:
                put_conn(conn)

    @requires_platform_db
    def test_deletion_of_an_account_without_paths_is_harmless(self, account):
        """没有路径的账号删除时不应报错（幂等、可重复调用）。"""
        from qwenpaw.app.user_space import _delete_account_paths

        _delete_account_paths(account)
        _delete_account_paths(account)
        assert P.list_path_tree(account) == []


class TestDeleteUserDataWiring:
    @requires_platform_db
    def test_delete_user_data_cleans_the_tree(self, account, tmp_path, monkeypatch):
        """``delete_user_data``（端点调用的那个函数）必须清掉路径树。

        单独测 ``_delete_account_paths`` 只证明"清得掉"，不证明**删账号时会调用它**。
        实测过：把调用注释掉，直接测 helper 的用例照样全绿 —— 所以这里从编排入口
        验证，才不会漏掉接线。
        """
        from qwenpaw.app import user_space

        # Keep the filesystem work inside tmp so the test cannot touch real accounts.
        monkeypatch.setattr(
            user_space, "get_user_root", lambda _u: tmp_path / "root",
        )
        monkeypatch.setattr(user_space, "USER_STATE_ROOT", tmp_path / "state")

        root = P.ensure_root_path(account)
        P.create_path(account, "案件一", parent_id=root)

        user_space.delete_user_data(account)

        assert P.list_path_tree(account) == [], "删账号没有清掉它的路径树"
