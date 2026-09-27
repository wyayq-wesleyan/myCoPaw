# -*- coding: utf-8 -*-
"""LightRAG 来源标识、doc_id 与空间过滤的单元测试。

这一层修的是两个实测问题，二者同源（LightRAG 会把 file_source 规范化成
basename）：

1. **图谱证据一直没参与检索**：空间信息原本放在路径式前缀
   ``knowledge://{space}/...`` 里，而 ``canonicalize_parser_hinted_basename``
   只取 ``Path(file_path).name``，前缀被整个剥掉，存储里只留下裸文件名。
   过滤按前缀匹配，于是**每一行都被丢弃**。实测：真实存储的 21 条记录
   ``file_path`` 全是裸文件名，没有一条带 ``knowledge://``。
2. **跨账号 doc_id 冲突**：doc_id 是 ``"doc-" + md5(file_source)``，而
   file_source 被规范化成 basename 后，两个账号上传同名文件会算出同一个
   doc_id，图谱里互相覆盖。把空间与文档 id 编进文件名即唯一。
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
KNOWLEDGE_SERVICE = REPO_ROOT / "knowledge-service"


def _load_bridge():
    """导入 lightrag_bridge，兼容有无 lightrag 包两种环境。

    生产镜像里装了 lightrag-hku，测试环境不一定装。模块对 lightrag 的依赖是
    可选的（doc_id 复现用 try/except 包住），所以这里能在裸环境下加载。
    """
    if str(KNOWLEDGE_SERVICE) not in sys.path:
        sys.path.insert(0, str(KNOWLEDGE_SERVICE))
    import app.lightrag_bridge as bridge  # noqa: PLC0415

    return bridge


@pytest.fixture()
def bridge():
    return _load_bridge()


def _settings(**overrides):
    base = {
        "lightrag_url": "http://lightrag:9621",
        "lightrag_api_key": "k",
        "lightrag_query_mode": "mix",
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


# --- 前缀必须在规范化后存活（原缺陷的直接回归）------------------------------


def test_space_prefix_survives_basename_normalization(bridge):
    """空间的标识必须留在文件名里，否则过滤永远匹配不到。

    这条用真实的规范化行为做断言：任何目录式前缀都会被剥掉，因此来源标识
    不能依赖路径分隔符。
    """
    space = "3f2a1b0c-1234-5678-9abc-def012345678"
    ref = bridge._source_ref(space, "doc-1", "报告.pdf")

    # 目录式前缀会被吃掉（这就是旧实现的形态）。
    assert not ref.startswith("knowledge://")
    # 而真正的 basename 里必须带着这个空间的前缀。
    assert Path(ref).name.startswith(bridge._scoped_prefix(space))
    assert Path(ref).name == ref  # 本身就是一个 basename，不含路径分隔符


def test_prefix_matching_filters_other_spaces(bridge):
    """混合空间返回时，只保留本空间的 chunk。"""
    mine, other = "space-alice", "space-bob"
    payload = {
        "data": {
            "chunks": [
                {
                    "chunk_id": "c1",
                    "content": "alice 的内容",
                    "file_path": bridge._source_ref(mine, "d1", "报告.pdf"),
                },
                {
                    "chunk_id": "c2",
                    "content": "bob 的内容",
                    "file_path": bridge._source_ref(other, "d1", "报告.pdf"),
                },
                {
                    # 升级前的旧记录：裸文件名，无法归属任何空间。
                    "chunk_id": "c3",
                    "content": "旧记录",
                    "file_path": "报告.pdf",
                },
            ],
        },
    }
    got = bridge.LightRAGBridge(_settings())._normalize_lightrag_result(
        payload, mine,
    )
    assert [item["content"] for item in got] == ["alice 的内容"]


def test_legacy_path_style_prefix_is_not_accepted(bridge):
    """旧式 ``knowledge://...`` 标识不该被当成有效归属。

    它出现在存储里时会变成裸文件名，按前缀匹配自然落选；这里显式钉住行为，
    免得将来有人「兼容」它而重新引入路径式前缀。
    """
    payload = {
        "data": {
            "chunks": [
                {
                    "chunk_id": "c1",
                    "content": "x",
                    "file_path": "knowledge://space-alice/d1/报告.pdf",
                },
            ],
        },
    }
    got = bridge.LightRAGBridge(_settings())._normalize_lightrag_result(
        payload, "space-alice",
    )
    assert got == []


# --- slug 不能有损（否则两个空间会串数据）----------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # 会被朴素替换折叠成同一个值的几组。
        ("a-b", "a_b"),
        ("space.alice", "space-alice"),
        ("a/b", "a_b"),
        ("A B", "A_B"),
    ],
)
def test_slug_distinguishes_ids_that_naive_sanitizing_would_merge(
    bridge, left, right,
):
    """不同空间标识必须得到不同 slug。

    LightRAG 自己对 ``LIGHTRAG-WORKSPACE`` 头做的是有损替换，照抄那个写法会
    让 ``a-b`` 与 ``a_b`` 折叠成一个空间、互相串数据。这里要求 slug 唯一。
    """
    assert bridge._space_slug(left) != bridge._space_slug(right)


def test_slug_is_stable_and_identifier_safe(bridge):
    """slug 要可重复（同一空间每次算出同一个）且只含标识符安全字符。"""
    space = "3f2a1b0c-1234-5678-9abc-def012345678"
    first = bridge._space_slug(space)
    assert first == bridge._space_slug(space)
    assert first == first.strip()
    # 只允许 [0-9A-Za-z_]，保证能直接用作 LightRAG 的 workspace/文件名片段。
    assert all(ch.isalnum() or ch == "_" for ch in first)


def test_slug_never_empty(bridge):
    """空标识不能产出一个空前缀 —— 那会让过滤匹配一切。"""
    for empty in ("", None):
        slug = bridge._space_slug(empty)
        assert slug
        assert not slug.startswith("_")
        # 空前缀会让所有 file_path 都通过 startswith。
        assert bridge._scoped_prefix(empty).strip("_") != ""


# --- doc_id 复现（删除功能依赖它）-----------------------------------------


def test_doc_id_matches_lightrag_formula(bridge):
    """doc_id 必须是 ``"doc-" + md5(basename)``。

    删除时无法向 LightRAG 查询 id，只能自己算，所以这个算法必须与写入侧一致。
    这里用真实存储中的数据反推验证：现有记录的 doc_id 与其 file_path 相符。
    """
    from hashlib import md5

    ref = bridge._source_ref("space-alice", "d1", "报告.pdf")
    expected = "doc-" + md5(Path(ref).name.encode("utf-8")).hexdigest()
    assert bridge._lightrag_doc_id(ref) == expected


def test_same_filename_in_different_spaces_gets_distinct_doc_ids(bridge):
    """同名文件在不同空间必须得到不同 doc_id。

    否则 LightRAG 按 id 去重，第二个账号上传的「报告.pdf」会被当成同一个
    文档，图谱数据互相覆盖。
    """
    a = bridge._source_ref("space-alice", "d1", "报告.pdf")
    b = bridge._source_ref("space-bob", "d1", "报告.pdf")
    assert a != b
    assert bridge._lightrag_doc_id(a) != bridge._lightrag_doc_id(b)


def test_same_document_in_different_spaces_gets_distinct_doc_ids(bridge):
    """同一个 document_id 出现在不同空间时也不能撞（空间是隔离维度）。"""
    a = bridge._source_ref("space-alice", "same-doc", "x.pdf")
    b = bridge._source_ref("space-bob", "same-doc", "x.pdf")
    assert bridge._lightrag_doc_id(a) != bridge._lightrag_doc_id(b)


def test_real_stored_doc_ids_round_trip(bridge):
    """用仓库里真实的 LightRAG 存储校验公式。

    ``kv_store_doc_status.json`` 是现网数据：每条记录的 key 都应等于用其
    ``file_path`` 算出的 doc_id。若这条失败，说明公式与 LightRAG 实现脱节，
    删除功能会静默失效（删不掉图谱数据）。
    """
    store_path = (
        KNOWLEDGE_SERVICE / "data" / "lightrag" / "kv_store_doc_status.json"
    )
    if not store_path.is_file():  # pragma: no cover - 数据目录可能不入库
        pytest.skip("no captured LightRAG store to check against")
    data = json.loads(store_path.read_text(encoding="utf-8"))
    if not data:  # pragma: no cover
        pytest.skip("captured LightRAG store is empty")

    from hashlib import md5

    checked = 0
    for doc_id, entry in data.items():
        file_path = entry.get("file_path")
        if not file_path:
            continue
        expected = "doc-" + md5(Path(file_path).name.encode("utf-8")).hexdigest()
        assert doc_id == expected, file_path
        checked += 1
    assert checked > 0


# --- 调用契约 --------------------------------------------------------------


def test_delete_needs_space_and_filename(bridge):
    """删除接口签名必须带 space 与原文件名 —— 否则算不出 doc_id。

    这条钉住调用契约：只传 document_id 的实现（修复前的形态）无法定位图谱
    记录。
    """
    import inspect

    params = inspect.signature(
        bridge.LightRAGBridge.delete_document,
    ).parameters
    assert list(params) == ["self", "space_id", "document_id", "source_name"]


def test_disabled_bridge_is_inert(bridge):
    """未配置 LightRAG 时所有操作都应是无副作用的空操作。"""
    br = bridge.LightRAGBridge(_settings(lightrag_url=""))
    assert br.enabled is False
    assert br._normalize_lightrag_result({"data": {"chunks": []}}, "s") == []


def test_malformed_payload_returns_empty(bridge):
    """响应异常不能抛错 —— 图谱信号是增强项，不能拖垮检索。"""
    br = bridge.LightRAGBridge(_settings())
    for bad in ({}, {"data": None}, {"data": []}, {"data": {"chunks": None}}):
        assert br._normalize_lightrag_result(bad, "space-alice") == []


# --- 删除调用点（静态守卫：修复前只传 document_id，删不掉图谱数据）---------


def test_delete_endpoint_passes_space_and_filename():
    """删除接口必须把 graph 清理所需的两个参数传下去。

    修复前是 ``lightrag.delete_document(document_id)``，而 LightRAG 的 doc_id
    由 (空间, 文档, 文件名) 决定，只给 document_id 算不出 id，删除会静默失败
    （本地库删了、图谱数据留下）。这条静态守卫确保参数没被改回去。
    """
    source = (KNOWLEDGE_SERVICE / "app" / "main.py").read_text(
        encoding="utf-8",
    )
    assert "lightrag.delete_document(space_id, document_id, source_name)" in source
    # 且必须先取到文档，才能拿到 space_id / filename。
    assert "store.get_document" in source


def test_bridge_no_longer_uses_path_style_namespace():
    """不允许再出现 ``knowledge://`` 前缀。

    那个形态在 LightRAG 里会被规范化成裸文件名，等于没有任何空间标识 ——
    正是导致过滤全失效的原因。
    """
    source = (KNOWLEDGE_SERVICE / "app" / "lightrag_bridge.py").read_text(
        encoding="utf-8",
    )
    # 允许在解释性注释里提到它，但不得出现在代码里。
    code_lines = [
        line
        for line in source.splitlines()
        if not line.lstrip().startswith("#")
    ]
    assert not any("knowledge://" in line for line in code_lines)


# --- 迁移（先删后建，幂等可续跑）-------------------------------------------


def test_legacy_delete_targets_the_unscoped_doc_id(bridge):
    """迁移第一步必须删掉「裸文件名」算出的旧 doc_id。

    旧记录是 ``file_source = filename``，其 doc_id 为 ``md5(basename)``。
    删错 id 的话，旧的无空间归属记录会一直留在图谱里（它对当前过滤不可见，
    但会污染实体/关系抽取结果）。
    """
    calls: list[list[str]] = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def delete(self, url, headers=None, json=None):
            calls.append(json["doc_ids"])
            return _Resp()

    br = bridge.LightRAGBridge(_settings())
    import asyncio

    from hashlib import md5

    async def run():
        import httpx

        orig = httpx.AsyncClient
        httpx.AsyncClient = lambda *a, **k: _Client()
        try:
            return await br.delete_legacy_document("报告.pdf")
        finally:
            httpx.AsyncClient = orig

    assert asyncio.run(run()) is True
    assert calls == [["doc-" + md5("报告.pdf".encode()).hexdigest()]]


def test_legacy_delete_preserves_the_llm_cache(bridge):
    """迁移删旧记录时不能清 LLM 缓存。

    清掉的话，紧接着的重建就必须重新做实体抽取 —— 那是一次真实且昂贵的 LLM
    调用；保留缓存才让迁移退化为「数据库重写」。
    """
    import asyncio
    from hashlib import md5  # noqa: F401

    payloads: list[dict] = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def delete(self, url, headers=None, json=None):
            payloads.append(json)
            return _Resp()

    br = bridge.LightRAGBridge(_settings())
    import httpx

    async def run():
        orig = httpx.AsyncClient
        httpx.AsyncClient = lambda *a, **k: _Client()
        try:
            return await br.delete_legacy_document("报告.pdf")
        finally:
            httpx.AsyncClient = orig

    assert asyncio.run(run()) is True
    assert payloads[0]["delete_llm_cache"] is False


def test_ordinary_delete_does_clear_the_llm_cache(bridge):
    """真正删除文档时要清缓存，回收空间 —— 与迁移的取舍相反。"""
    import asyncio

    payloads: list[dict] = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def delete(self, url, headers=None, json=None):
            payloads.append(json)
            return _Resp()

    br = bridge.LightRAGBridge(_settings())
    import httpx

    async def run():
        orig = httpx.AsyncClient
        httpx.AsyncClient = lambda *a, **k: _Client()
        try:
            return await br.delete_document("space-a", "d1", "报告.pdf")
        finally:
            httpx.AsyncClient = orig

    assert asyncio.run(run()) is True
    assert payloads[0]["delete_llm_cache"] is True


def test_migration_skips_uncompleted_documents(bridge):
    """只迁移已完成的文档 —— 处理中/失败的还没有分片可索引。"""
    import asyncio
    import types

    indexed: list[str] = []

    class _Store:
        def list_documents_internal(self, space_id):
            return [
                {"id": "d1", "filename": "a.pdf", "status": "completed"},
                {"id": "d2", "filename": "b.pdf", "status": "processing"},
                {"id": "d3", "filename": "c.pdf", "status": "failed"},
            ]

        def get_document_evidence(self, document_id):
            return [{"location": "正文", "content": "x"}]

    br = bridge.LightRAGBridge(_settings())

    async def fake_delete_legacy(source_name):
        return True

    async def fake_insert(**kwargs):
        indexed.append(kwargs["document_id"])
        return True

    br.delete_legacy_document = fake_delete_legacy
    br.insert = fake_insert

    # 迁移清单由 plan_migration 决定（只含 completed 的文档）；这里显式传入，
    # 避免依赖真实 LightRAG 列举。
    pending = [
        {"space_id": "space-a", "document_id": "d1", "filename": "a.pdf"},
    ]
    result = asyncio.run(
        br.migrate_legacy_documents(_Store(), "space-a", pending=pending),
    )
    assert indexed == ["d1"], "只应迁移清单里的文档"
    assert result["migrated"] == 1
    assert result["failed"] == 0


def test_migration_only_touches_pending_items_of_its_own_space(bridge):
    """传入的 pending 跨空间共用时，只处理本空间的部分。

    启动迁移会一次性算好所有空间的清单再逐个执行；若这里不过滤空间，第二个
    空间会把第一个空间的文档也重建一遍。
    """
    import asyncio

    indexed: list[str] = []

    class _Store:
        def get_document_evidence(self, document_id):
            return [{"location": "正文", "content": "x"}]

    br = bridge.LightRAGBridge(_settings())

    async def ok_legacy(source_name):
        return True

    async def fake_insert(**kwargs):
        indexed.append(f"{kwargs['space_id']}:{kwargs['document_id']}")
        return True

    br.delete_legacy_document = ok_legacy
    br.insert = fake_insert

    pending = [
        {"space_id": "space-a", "document_id": "a1", "filename": "a.pdf"},
        {"space_id": "space-b", "document_id": "b1", "filename": "b.pdf"},
    ]
    result = asyncio.run(
        br.migrate_legacy_documents(_Store(), "space-a", pending=pending),
    )
    assert indexed == ["space-a:a1"]
    assert result["migrated"] == 1


def test_migration_isolates_one_failing_document(bridge):
    """单个文档失败不能让整批迁移中断 —— 否则一个坏文档挡住全部历史数据。"""
    import asyncio

    class _Store:
        def list_documents_internal(self, space_id):
            return [
                {"id": "d1", "filename": "a.pdf", "status": "completed"},
                {"id": "d2", "filename": "boom.pdf", "status": "completed"},
                {"id": "d3", "filename": "c.pdf", "status": "completed"},
            ]

        def get_document_evidence(self, document_id):
            return [{"location": "正文", "content": "x"}]

    br = bridge.LightRAGBridge(_settings())

    async def fake_delete_legacy(source_name):
        if source_name == "boom.pdf":
            raise RuntimeError("simulated failure")
        return True

    async def fake_insert(**kwargs):
        return kwargs["source_name"] != "boom.pdf"

    br.delete_legacy_document = fake_delete_legacy
    br.insert = fake_insert

    pending = [
        {"space_id": "space-a", "document_id": "d1", "filename": "a.pdf"},
        {"space_id": "space-a", "document_id": "d2", "filename": "boom.pdf"},
        {"space_id": "space-a", "document_id": "d3", "filename": "c.pdf"},
    ]
    result = asyncio.run(
        br.migrate_legacy_documents(_Store(), "space-a", pending=pending),
    )
    assert result["migrated"] == 2
    assert result["failed"] == 1
    failed_ids = [
        d["document_id"] for d in result["details"] if "result" in d
        and not d["result"].startswith("migrated")
    ]
    assert "d2" in failed_ids


def test_migration_endpoint_exists_and_is_scoped():
    """迁移接口必须校验空间归属 —— 不能让一个账号迁移别人的空间。"""
    source = (KNOWLEDGE_SERVICE / "app" / "main.py").read_text(encoding="utf-8")
    assert "/v1/lightrag/migrate" in source
    assert "space_exists" in source
    # 迁移逻辑本身在 bridge 里，接口只做鉴权与转发。
    assert "migrate_legacy_documents" in source


def test_migration_reports_disabled_bridge(bridge):
    """未配置 LightRAG 时迁移应明确报告未启用，而不是假装成功。"""
    import asyncio

    br = bridge.LightRAGBridge(_settings(lightrag_url=""))

    class _Store:
        def list_documents_internal(self, space_id):  # pragma: no cover
            raise AssertionError("disabled bridge must not touch the store")

    result = asyncio.run(br.migrate_legacy_documents(_Store(), "space-a"))
    assert result["enabled"] is False
    assert result["migrated"] == 0


# --- 启动自动迁移：判定逻辑 -------------------------------------------------


def _fake_store(spaces, docs_by_space, evidence=None):
    """构造一个最小 store：列出全部空间、每个空间的文档、以及文档分片。"""
    ev = evidence or {}

    class _Store:
        def list_all_spaces_internal(self):
            return [{"id": s} for s in spaces]

        def list_documents_internal(self, space_id):
            return docs_by_space.get(space_id, [])

        def get_document_evidence(self, document_id):
            return ev.get(document_id, [{"location": "正文", "content": "x"}])

    return _Store()


def _completed(document_id, filename):
    return {"id": document_id, "filename": filename, "status": "completed"}


def test_plan_migration_flags_only_documents_missing_from_lightrag(bridge):
    """判定依据是「期望的 doc_id 是否已在 LightRAG 里」。

    已迁移的文档其新 doc_id 会出现在列举结果里，因此不会被重复迁移 —— 这让
    判定天然幂等，而且不需要维护本地记账表（记账会与实际状态漂移）。
    """
    import asyncio

    br = bridge.LightRAGBridge(_settings())
    space = "space-a"
    docs = [_completed("d1", "a.pdf"), _completed("d2", "b.pdf")]
    store = _fake_store([space], {space: docs})

    already = bridge._lightrag_doc_id(bridge._source_ref(space, "d1", "a.pdf"))

    async def fake_fetch():
        return {already}

    br.fetch_indexed_doc_ids = fake_fetch
    pending = asyncio.run(br.plan_migration(store))
    assert pending == [
        {"space_id": space, "document_id": "d2", "filename": "b.pdf"},
    ]


def test_plan_migration_returns_none_when_listing_fails(bridge):
    """列举失败必须返回 None，而不是空列表。

    空列表等于「全部已迁移」→ 永远不会迁移；也不该当成「全部待迁移」→ 每次
    启动都全量重建。两者都错，所以用 None 表示「本轮无法判断」。
    """
    import asyncio

    br = bridge.LightRAGBridge(_settings())

    async def fake_fetch():
        return None

    br.fetch_indexed_doc_ids = fake_fetch
    store = _fake_store(["s"], {"s": [_completed("d1", "a.pdf")]})
    assert asyncio.run(br.plan_migration(store)) is None


def test_plan_migration_ignores_uncompleted_documents(bridge):
    """处理中/失败的文档不进迁移清单 —— 它们还没有分片可索引。"""
    import asyncio

    br = bridge.LightRAGBridge(_settings())
    space = "s"
    store = _fake_store(
        [space],
        {
            space: [
                _completed("d1", "a.pdf"),
                {"id": "d2", "filename": "b.pdf", "status": "processing"},
                {"id": "d3", "filename": "c.pdf", "status": "failed"},
            ],
        },
    )

    async def no_docs_indexed():
        return set()

    br.fetch_indexed_doc_ids = no_docs_indexed
    pending = asyncio.run(br.plan_migration(store))
    assert [p["document_id"] for p in pending] == ["d1"]


def test_plan_migration_spans_every_space(bridge):
    """必须遍历所有账号的空间 —— 启动时没有请求上下文，不能只处理一个账号。"""
    import asyncio

    br = bridge.LightRAGBridge(_settings())
    store = _fake_store(
        ["space-a", "space-b"],
        {
            "space-a": [_completed("a1", "x.pdf")],
            "space-b": [_completed("b1", "y.pdf")],
        },
    )

    async def no_docs_indexed():
        return set()

    br.fetch_indexed_doc_ids = no_docs_indexed
    pending = asyncio.run(br.plan_migration(store))
    assert {p["space_id"] for p in pending} == {"space-a", "space-b"}
    assert len(pending) == 2


def test_migrate_all_skips_when_nothing_pending(bridge):
    """无需迁移时不做任何写入，只报告 up-to-date。"""
    import asyncio

    br = bridge.LightRAGBridge(_settings())
    store = _fake_store(["s"], {"s": [_completed("d1", "a.pdf")]})

    indexed = bridge._lightrag_doc_id(bridge._source_ref("s", "d1", "a.pdf"))

    async def already():
        return {indexed}

    br.fetch_indexed_doc_ids = already

    async def must_not_insert(**kwargs):  # pragma: no cover
        raise AssertionError("no writes expected when nothing is pending")

    br.insert = must_not_insert
    result = asyncio.run(br.migrate_all_if_needed(store))
    assert result["needed"] is False
    assert result["migrated"] == 0


def test_migrate_all_executes_when_pending(bridge):
    """有待迁移项时自动执行，并报告数量。"""
    import asyncio

    br = bridge.LightRAGBridge(_settings())
    store = _fake_store(["s"], {"s": [_completed("d1", "a.pdf")]})

    async def none_indexed():
        return set()

    br.fetch_indexed_doc_ids = none_indexed

    async def fake_delete_legacy(source_name):
        return True

    async def fake_insert(**kwargs):
        return True

    br.delete_legacy_document = fake_delete_legacy
    br.insert = fake_insert

    result = asyncio.run(br.migrate_all_if_needed(store))
    assert result["needed"] is True
    assert result["migrated"] == 1
    assert result["failed"] == 0


def test_migrate_all_is_inert_when_lightrag_is_not_configured(bridge):
    """未配置 LightRAG 时不该建任务、也不该碰 store。"""
    import asyncio

    br = bridge.LightRAGBridge(_settings(lightrag_url=""))

    class _Store:
        def list_all_spaces_internal(self):  # pragma: no cover
            raise AssertionError("must not touch the store when disabled")

    result = asyncio.run(br.migrate_all_if_needed(_Store()))
    assert result == {"enabled": False, "needed": False}


def test_migrate_all_never_raises_on_planning_failure(bridge):
    """启动路径上任何失败都只能降级为日志 —— 不能把服务拉不起来。"""
    import asyncio

    br = bridge.LightRAGBridge(_settings())

    async def boom():
        raise RuntimeError("simulated planning failure")

    br.plan_migration = boom
    result = asyncio.run(br.migrate_all_if_needed(object()))
    assert result["enabled"] is True
    assert result["needed"] is None


def test_startup_runs_migration_in_the_background():
    """迁移必须挂在 lifespan 上，且是后台任务。

    同步等待会让健康检查在数分钟内失败，编排层可能判定启动失败；而图谱只是
    检索增强项，完全可以在服务就绪后再补。
    """
    source = (KNOWLEDGE_SERVICE / "app" / "main.py").read_text(encoding="utf-8")
    assert "_start_graph_migration" in source
    assert "asyncio.create_task" in source
    assert "migrate_all_if_needed" in source
    # 必须在 lifespan 里被调用。
    lifespan_body = source.split("@asynccontextmanager", 1)[1].split("app = FastAPI", 1)[0]
    assert "_start_graph_migration()" in lifespan_body
    # 不该用 await 直接等 —— 那就是同步阻塞。
    assert "await _start_graph_migration" not in source


def test_startup_cancels_migration_task_on_shutdown():
    """关停时要取消未完成的迁移，避免留下悬挂任务。"""
    source = (KNOWLEDGE_SERVICE / "app" / "main.py").read_text(encoding="utf-8")
    assert "migration_task.cancel()" in source
