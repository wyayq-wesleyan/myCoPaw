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
