# -*- coding: utf-8 -*-
"""Rank fusion and reranking contracts.

The old retriever concatenated per-source lists in a fixed order and truncated:
a chunk found by one weak lexical route could push every semantic hit out of the
answer, and a filename hit could only survive because it was inserted first.
RRF makes the ordering a property of relevance and keeps every source in the
same ranking.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_APP = pathlib.Path(__file__).resolve().parents[3] / "knowledge-service" / "app"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"ks_{name}", _APP / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fusion = _load("fusion")


def _row(row_id: str, source: str, score: float = 0.5) -> dict:
    return {
        "id": row_id,
        "document_id": f"doc-{row_id}",
        "page": 1,
        "location": "正文",
        "content": f"content {row_id}",
        "filename": f"{row_id}.txt",
        "source": source,
        "score": score,
    }


def test_multi_source_item_outranks_single_source_item():
    fused = fusion.fuse(
        {
            "literal": [_row("a", "literal", 0.99), _row("b", "literal", 0.99)],
            "semantic": [_row("b", "semantic", 0.8), _row("c", "semantic", 0.8)],
        }
    )

    assert fused[0]["id"] == "b"
    assert set(fused[0]["match_reasons"]) == {"literal", "semantic"}
    assert fused[0]["confidence"] == "high"


def test_semantic_hits_are_not_dropped_when_lexical_fills_the_list():
    """The regression this replaces: top_k lexical rows hid every semantic row."""

    ranked = {
        "literal": [_row(f"l{i}", "literal", 0.9) for i in range(10)],
        "semantic": [_row("semantic-only", "semantic", 0.8)],
    }
    fused = fusion.fuse(ranked)

    ids = [item["id"] for item in fused]
    assert "semantic-only" in ids
    # A single semantic hit scores ~0.8/61; the 10th lexical row scores
    # ~0.9/70. The semantic hit therefore sorts above the tail of a long list
    # instead of being truncated away.
    assert ids.index("semantic-only") < ids.index("l9")


def test_filename_weight_is_preserved():
    fused = fusion.fuse(
        {
            "filename": [_row("f", "filename")],
            "semantic": [_row("s", "semantic")],
        }
    )
    by_id = {item["id"]: item for item in fused}
    assert by_id["f"]["fusion_score"] > by_id["s"]["fusion_score"]
    assert by_id["f"]["source"] == "filename"


def test_same_chunk_from_two_routes_is_merged_once():
    fused = fusion.fuse(
        {
            "filename": [_row("same", "filename")],
            "semantic": [_row("same", "semantic")],
        }
    )
    assert len(fused) == 1
    assert set(fused[0]["match_reasons"]) == {"filename", "semantic"}


def test_reranker_order_is_applied_and_missing_rows_are_kept():
    fused = [_row("a", "literal"), _row("b", "literal"), _row("c", "literal")]

    reordered = fusion.apply_rerank(fused, [(2, 0.9), (0, 0.8)])

    assert [item["id"] for item in reordered] == ["c", "a", "b"]
    assert reordered[0]["rerank_score"] == 0.9
    # The reranker may return only top_n rows; the rest must not disappear.
    assert "b" in [item["id"] for item in reordered]


def test_missing_reranker_result_keeps_fused_order():
    fused = [_row("a", "literal"), _row("b", "literal")]
    assert fusion.apply_rerank(fused, None) == fused
