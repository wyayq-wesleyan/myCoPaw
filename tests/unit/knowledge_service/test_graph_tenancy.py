# -*- coding: utf-8 -*-
"""知识图谱多租户隔离（workspace per path）。

设计见 ``docs/知识服务多租户存储设计方案.md``。这里锁定的是**隔离契约**，也就是
改造的真正目的：跨路径读取必须在存储层做不到，而不是"取回来再过滤"。

真实数据库上的插入/检索/删除已在实施时端到端验证过（两个 workspace、同名实体、
多挂载合并、删除隔离）。本文件覆盖不依赖数据库的部分 —— 命名、合并、映射，
这些是纯函数，正是最容易写错而又不需要真库就能锁死的地方。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

# The knowledge service is deployed separately (its own app package), so it is
# imported by path rather than as part of this repo's package tree.
_APP = pathlib.Path(__file__).resolve().parents[3] / "knowledge-service" / "app"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"ks_{name}", _APP / f"{name}.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def graph_store():
    return _load("graph_store")


class TestWorkspaceNaming:
    def test_path_id_maps_to_a_stable_workspace(self, graph_store):
        """路径 id → workspace 名，稳定且只含安全字符。

        LightRAG 的 ``validate_workspace`` 会拒绝含 ``/``、``\\``、``.``、``..`` 的
        名字；UUID 去掉连字符后只含 ``[0-9a-f]``，天然满足，不需要转义（转义会带来
        「两个不同路径被归一成同一名字」的风险）。
        """
        path_id = "db91be64-95fa-4125-a797-58c7e076121f"
        ws = graph_store.workspace_for_path(path_id)
        assert ws == "ws_db91be6495fa4125a79758c7e076121f"
        # 幂等：同一路径永远得到同一个名字，否则重启后读不到自己的图谱。
        assert graph_store.workspace_for_path(path_id) == ws
        assert "/" not in ws and "." not in ws

    def test_distinct_paths_never_collide(self, graph_store):
        """不同路径必须得到不同 workspace。

        碰撞意味着两个案件共用一张图 —— 正是要防的事故。
        """
        pairs = [
            ("11111111-1111-1111-1111-111111111111", "11111111-1111-1111-1111-111111111112"),
            ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        ]
        names = {graph_store.workspace_for_path(a) for a, _ in pairs}
        # 前两个不同；第二个 pair 的两个 id 相同，应当归一。
        assert len(names) == 2
        assert graph_store.workspace_for_path(pairs[1][0]) == graph_store.workspace_for_path(pairs[1][1])

    def test_invalid_ids_are_rejected_not_silently_coerced(self, graph_store):
        """非法 id 直接报错，不要"尽力而为"地产出一个名字。

        静默把非法字符替换掉，会让 ``a-b`` 与 ``a_b`` 落到同一个 workspace，
        两个案件就此串数据。
        """
        for bad in ("", "not-a-uuid", "ws_x", "11111111-1111-1111-1111-11111111111z"):
            with pytest.raises(ValueError):
                graph_store.workspace_for_path(bad)


class TestEvidenceParsing:
    def test_file_path_is_parsed_back_into_document_and_name(self, graph_store):
        """``file_path`` 的格式是 ``<document_id>__<filename>``，要能还原。

        证据必须能溯源到具体文件；还原失败会让「图谱命中」变成无出处的片段。
        """
        tenant_cls = graph_store.TenantGraph
        payload = {
            "data": {
                "chunks": [
                    {"chunk_id": "c1", "content": "内容", "file_path": "doc-1__报告.pdf", "score": 0.9},
                    {"chunk_id": "c2", "content": "内容2", "file_path": "doc-2__无分隔符"},
                ],
            },
        }
        rows = tenant_cls._to_evidence(payload, "ws_x")
        assert [r["document_id"] for r in rows] == ["doc-1", "doc-2"]
        assert rows[0]["filename"] == "报告.pdf"
        # A path with no separator must not crash or invent an id.
        assert rows[1]["filename"] == "无分隔符"
        assert all(r["workspace"] == "ws_x" for r in rows)

    def test_document_id_is_not_fabricated_when_absent(self, graph_store):
        """没有分隔符时 document_id 应为 None，而不是把整串当成 id。"""
        rows = graph_store.TenantGraph._to_evidence(
            {"data": {"chunks": [{"content": "x", "file_path": "plain.txt"}]}}, "ws_x",
        )
        assert rows[0]["document_id"] is None


class TestCrossWorkspaceMerge:
    def test_results_from_every_workspace_are_kept(self, graph_store):
        """多挂载：每个 workspace 的结果都要出现。

        对应「挂了却没查到」——若合并只保留一个来源，用户会以为挂载没生效。
        """
        batches = [
            [{"id": "a1", "score": 0.5, "workspace": "ws_a"}],
            [{"id": "b1", "score": 0.5, "workspace": "ws_b"}],
        ]
        merged = graph_store.TenantGraph._merge(batches, top_k=10)
        assert {m["workspace"] for m in merged} == {"ws_a", "ws_b"}

    def test_scores_are_normalised_so_one_workspace_cannot_drown_the_others(self, graph_store):
        """分数归一：否则绝对分高的 workspace 会淹没另一个。

        两个 workspace 的分数量纲不同，直接比较会让某一边的结果全被挤掉 ——
        表现仍然是「挂了另一个却没查到」。
        """
        batches = [
            [{"id": "a1", "score": 0.99, "workspace": "ws_a"}],
            [{"id": "b1", "score": 0.01, "workspace": "ws_b"}],
        ]
        merged = graph_store.TenantGraph._merge(batches, top_k=10)
        assert len(merged) == 2, "另一个 workspace 的结果被挤掉了"
        assert all(abs(m["score"] - 1.0) < 1e-9 for m in merged)

    def test_duplicates_are_dropped(self, graph_store):
        """同一 workspace 的同一条证据不应重复计数。"""
        batches = [[{"id": "a1", "score": 1.0, "workspace": "ws_a"}]] * 3
        assert len(graph_store.TenantGraph._merge(batches, top_k=10)) == 1

    def test_top_k_is_respected(self, graph_store):
        batches = [[{"id": f"x{i}", "score": 1.0 - i / 100, "workspace": "ws"} for i in range(10)]]
        assert len(graph_store.TenantGraph._merge(batches, top_k=3)) == 3

    def test_empty_workspace_list_yields_nothing(self, graph_store):
        """空列表 = 无权限 = 空结果（不能退化成"不过滤"）。"""
        assert graph_store.TenantGraph._merge([], top_k=5) == []


class TestFailClosed:
    def test_pool_disabled_when_unconfigured(self, graph_store):
        """未配置图谱库时 ``enabled`` 为 False，调用方据此拒绝而不是放行。"""

        class _S:
            graph_database_url = ""
            data_dir = "/tmp"
            graph_max_workspaces = 4
            graph_pool_max_connections = 2

        pool = graph_store.GraphPool(_S())
        assert pool.enabled is False

    def test_unavailable_store_raises_instead_of_returning_everything(self, graph_store):
        """不可用的存储必须抛错，而不是返回"全部"。

        检索侧把它当「没有图谱数据」（少一个信号），绝不能当「没有限制」。
        """

        class _S:
            graph_database_url = ""
            data_dir = "/tmp"
            graph_max_workspaces = 4
            graph_pool_max_connections = 2

        pool = graph_store.GraphPool(_S())
        with pytest.raises(graph_store.GraphStoreUnavailable):
            import asyncio

            asyncio.run(pool.get("ws_x"))
