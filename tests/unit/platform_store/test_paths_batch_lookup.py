# -*- coding: utf-8 -*-
"""资源归属必须支持批量查询，否则「分配资源」对话框会卡住整个后端。

`path_of_resource` 一次只回答一个 id。`GET /paths/assignable` 要为账号下**每个**
文档/数据表都问一次「它属于哪个案件」：1000 个文档 = 1000 次串行查询，而且全部
跑在事件循环上（端点虽是 async，调用的是同步 psycopg2）。平台池满时每次取连接
还会 sleep 自旋，整页跟着卡住。

`paths_of_resources` 用 `= ANY(%s)` 一次查完。这里锁住两点：
1. 批量函数存在且按 `resource_kind` 过滤；
2. 端点用的是批量函数，不是逐行 `path_of_resource`。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PATHS_STORE = REPO_ROOT / "src" / "qwenpaw" / "platform_store" / "paths.py"
PATHS_ROUTER = REPO_ROOT / "src" / "qwenpaw" / "app" / "routers" / "paths.py"

try:
    import psycopg2  # noqa: F401

    _HAS_PG = True
except ImportError:  # pragma: no cover
    _HAS_PG = False

import os

_DSN = os.environ.get("QWENPAW_TEST_PLATFORM_DB", "").strip()


def test_batch_lookup_exists_and_uses_any() -> None:
    src = PATHS_STORE.read_text(encoding="utf-8")
    i = src.index("def paths_of_resources(")
    j = src.index("\ndef ", i + 1)
    body = src[i:j]
    assert "ANY(%s)" in body, "批量查询没有用 = ANY(%s)，仍然是一次一个"
    assert "resource_kind = %s" in body, "批量查询必须仍按类型过滤"
    assert "if not resource_ids" in body, "空输入应短路，避免空数组查询"


def test_assignable_endpoint_uses_the_batch_lookup() -> None:
    """端点里不能残留逐行的 path_of_resource。"""
    src = PATHS_ROUTER.read_text(encoding="utf-8")
    i = src.index("async def list_assignable_resources(")
    j = src.index("\n@router.", i)
    body = src[i:j]
    assert "paths_of_resources" in body, "端点还在用逐行查询"
    assert not re.search(
        r'"current_path_id":\s*path_store\.path_of_resource', body,
    ), "端点里仍有逐行 path_of_resource：1000 个资源就是 1000 次查询"
    # 逐行版本只应出现在单个资源的操作里（分配/移动），不在列表里
    assert body.count("path_store.path_of_resource(") == 0


@pytest.mark.skipif(not (_DSN and _HAS_PG), reason="set QWENPAW_TEST_PLATFORM_DB=1")
class TestAgainstRealPostgres:
    def test_batch_and_single_agree(self) -> None:
        import uuid

        from qwenpaw.platform_store import paths

        suffix = uuid.uuid4().hex[:8]
        owner = f"batch-{suffix}"
        root = paths.ensure_root_path(owner)
        sub = paths.create_path(owner, f"case-{suffix}", parent_id=root)
        bound_id = f"res-{suffix}-1"
        free_id = f"res-{suffix}-2"
        paths.bind_resource(
            path_id=sub, resource_kind=paths.KIND_DATA_TABLE, resource_id=bound_id,
        )
        try:
            batch = paths.paths_of_resources(
                paths.KIND_DATA_TABLE, [bound_id, free_id],
            )
            assert batch.get(bound_id) == sub
            assert free_id not in batch, "未绑定的资源不应出现（调用方按缺失判为公共区）"
            # 与单条查询一致
            assert paths.path_of_resource(paths.KIND_DATA_TABLE, bound_id) == sub
            assert paths.path_of_resource(paths.KIND_DATA_TABLE, free_id) is None
        finally:
            paths.unbind_resource(
                resource_kind=paths.KIND_DATA_TABLE, resource_id=bound_id,
            )
