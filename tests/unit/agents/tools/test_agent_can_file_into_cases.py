# -*- coding: utf-8 -*-
"""智能体必须能把数据放进案件（路径），而不是只能放进默认区。

用户报的问题：让智能体把数据上传到「数据空间-xxx案件」下，它做不到，只能落到
默认路径。根因不在隔离设计（隔离本身是好的），而在**工具层缺参数**：后端的
``create_table_record`` 一直支持 ``path_id``，但 ``create_data_table`` 工具没有
这个参数，所以建出来的表永远是「未绑定 = 公共区」。

更糟的是这会造成自锁：智能体若只被授权了案件，它新建的表落在公共区，于是它自己
既列不出也查不到刚建的表。

这些测试锁定新的契约：
1. 路径可以用「案件名称」或 id 指定；
2. 只能在**被授权**的路径里解析（防止把数据写进没授权的案件）；
3. 重名时报候选而不是猜；
4. 未授权/不存在的名称被拒绝，且不会真的建出表。
"""
from __future__ import annotations

import pytest

from qwenpaw.agents.tools import path_scope as ps


class _FakePathsStore:
    """Minimal platform_store.paths stand-in."""

    def __init__(self, paths: dict[str, dict], grants: list[str]):
        self._paths = paths
        self._grants = grants

    def agent_path_ids(self, agent_id: str) -> list[str]:
        return list(self._grants)

    def path_by_id(self, path_id: str):
        return self._paths.get(path_id)


def _node(path_id: str, name: str, *, is_root=False, parent=None):
    return {
        "id": path_id,
        "name": name,
        "is_root": is_root,
        "parent_id": parent,
        "owner_username": "admin",
    }


@pytest.fixture
def store(monkeypatch):
    """Install a fake path store and a known agent identity."""
    nodes = {
        "root-1": _node("root-1", "默认", is_root=True),
        "case-A": _node("case-A", "案件A", parent="root-1"),
        "case-B": _node("case-B", "2024销售案件", parent="root-1"),
        "other": _node("other", "别人的案件", parent="root-1"),
    }
    fake = _FakePathsStore(nodes, ["root-1", "case-A", "case-B"])

    import qwenpaw.platform_store.paths as real

    monkeypatch.setattr(real, "agent_path_ids", fake.agent_path_ids)
    monkeypatch.setattr(real, "path_by_id", fake.path_by_id)
    monkeypatch.setattr(ps, "current_agent_id", lambda: "agent-1")
    return fake


class TestResolvingAGrantedPath:
    def test_empty_reference_means_the_default_area(self, store):
        """没指定案件时保持原行为：落到默认区。"""
        assert ps.resolve_writable_path_id("admin", "") == ""
        assert ps.resolve_writable_path_id("admin", "   ") == ""

    def test_the_root_path_also_resolves_to_the_default_area(self, store):
        """显式指定默认区时同样映射为空——它不是一个独立 schema。"""
        assert ps.resolve_writable_path_id("admin", "root-1") == ""

    def test_case_name_resolves_to_its_id(self, store):
        assert ps.resolve_writable_path_id("admin", "案件A") == "case-A"

    def test_case_id_resolves_to_itself(self, store):
        assert ps.resolve_writable_path_id("admin", "case-A") == "case-A"

    def test_partial_name_matches(self, store):
        """模型多半只会说「销售案件」，不该因此失败。"""
        assert ps.resolve_writable_path_id("admin", "销售案件") == "case-B"

    def test_partial_match_is_case_insensitive(self, store):
        store._paths["case-C"] = _node("case-C", "ProjectX", parent="root-1")
        store._grants.append("case-C")
        assert ps.resolve_writable_path_id("admin", "projectx") == "case-C"


class TestIsolationOnTheWriteSide:
    """写侧隔离：不能把数据放进没有被授权的案件。"""

    def test_a_case_the_agent_was_not_granted_is_refused(self, store):
        """「别人的案件」存在于账号里，但没有授权给这个智能体。"""
        with pytest.raises(ps.PathNotWritable) as exc:
            ps.resolve_writable_path_id("admin", "别人的案件")
        # The message must name what IS available, so the model can correct itself.
        assert "案件A" in str(exc.value)

    def test_an_unknown_name_is_refused(self, store):
        with pytest.raises(ps.PathNotWritable):
            ps.resolve_writable_path_id("admin", "不存在的案件")

    def test_an_ungranted_id_is_refused(self, store):
        """直接给 id 也不能绕过授权检查。"""
        with pytest.raises(ps.PathNotWritable):
            ps.resolve_writable_path_id("admin", "other")

    def test_no_grants_denies_instead_of_falling_back_to_public(self, store):
        """fail-closed：没有任何授权时不写任何地方，而不是写公共区。"""
        store._grants = []
        with pytest.raises(ps.PathNotWritable):
            ps.resolve_writable_path_id("admin", "案件A")

    def test_unknown_agent_identity_denies(self, store, monkeypatch):
        monkeypatch.setattr(ps, "current_agent_id", lambda: "")
        with pytest.raises(ps.PathNotWritable):
            ps.resolve_writable_path_id("admin", "案件A")


class TestAmbiguousNamesAreReportedNotGuessed:
    """重名时不能猜 —— 猜错等于把用户的数据静默放错地方。"""

    def test_two_cases_with_the_same_name_return_candidates(self, monkeypatch):
        nodes = {
            "root-1": _node("root-1", "默认", is_root=True),
            "dup-1": _node("dup-1", "重复案件", parent="root-1"),
            "dup-2": _node("dup-2", "重复案件", parent="root-1"),
        }
        fake = _FakePathsStore(nodes, ["root-1", "dup-1", "dup-2"])
        import qwenpaw.platform_store.paths as real

        monkeypatch.setattr(real, "agent_path_ids", fake.agent_path_ids)
        monkeypatch.setattr(real, "path_by_id", fake.path_by_id)
        monkeypatch.setattr(ps, "current_agent_id", lambda: "agent-1")

        with pytest.raises(ps.PathAmbiguous) as exc:
            ps.resolve_writable_path_id("admin", "重复案件")
        ids = {c["id"] for c in exc.value.candidates}
        assert ids == {"dup-1", "dup-2"}
        # Both candidates must be surfaced so the user can choose.
        assert "dup-1" in str(exc.value) and "dup-2" in str(exc.value)


class TestGrantedPathListing:
    def test_lists_names_and_root_marker(self, store):
        nodes = ps.granted_path_nodes("admin")
        assert nodes is not None
        names = {n["name"] for n in nodes}
        assert names == {"默认", "案件A", "2024销售案件"}
        assert any(n["is_root"] for n in nodes)

    def test_a_grant_pointing_at_a_deleted_path_is_skipped(self, store):
        """已删除的路径授权不该拖垮其余案件。"""
        store._grants.append("ghost")
        nodes = ps.granted_path_nodes("admin")
        assert nodes is not None
        assert {n["id"] for n in nodes} == {"root-1", "case-A", "case-B"}

    def test_no_grants_returns_none_not_empty(self, store):
        """``None`` 表示拒绝；空列表会被误读成「不过滤」。"""
        store._grants = []
        assert ps.granted_path_nodes("admin") is None
