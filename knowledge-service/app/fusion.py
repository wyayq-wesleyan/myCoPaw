"""Rank fusion for the hybrid retriever.

The previous implementation concatenated the per-source result lists in a fixed
order (filename → entity → fulltext → semantic) and truncated. That made the
ranking a property of the code layout rather than of relevance: a single weak
lexical hit could push every semantic hit out of the answer, and the caller had
no way to tell.

Reciprocal Rank Fusion (RRF) fixes the shape of the problem without pretending
that ``ts_rank_cd``, cosine similarity and bigram coverage are the same scale:

    score(item) = Σ_source weight(source) / (k + rank_in_source)

``k`` damps the top ranks (the standard 60 is a good default for heterogeneous
retrievers). Source weights keep the project's measured preferences — a filename
hit is stronger evidence than a fuzzy bigram hit — while still letting an item
that appears in several lists outrank any single-list item.

The reranker, when configured, runs *after* fusion on the fused candidates; it
is the only stage that sees the query and the chunk text together.
"""

from __future__ import annotations

from typing import Any

# Relative trust in each route, carried over from the measured behaviour in
# README §9.24: filename information is otherwise invisible, full-text is exact,
# literal bigrams are noisy, semantic is broad, entity/relation is weak for
# natural-language questions.
SOURCE_WEIGHTS: dict[str, float] = {
    "fulltext": 1.0,
    "filename": 0.95,
    "literal": 0.9,
    "semantic": 0.8,
    "entity": 0.7,
    "lightrag": 0.7,
}

RRF_K = 60
# Reranking is comparatively expensive; keep the candidate set bounded even if
# the caller asks for a large top_k.
MAX_FUSED_CANDIDATES = 120


def item_key(item: dict[str, Any]) -> str:
    """Stable identity for one evidence row.

    Chunk ids are authoritative. Graph rows have no chunk id, so their
    location/filename is used; that is also what makes a graph row dedupe
    against itself across repeated calls.
    """
    return str(
        item.get("id")
        or f"{item.get('document_id')}:{item.get('page')}:{item.get('location')}"
    )


def fuse(ranked_lists: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Fuse ``{source: ranked rows}`` into one list ordered by RRF score.

    Rows are merged by :func:`item_key`; the highest raw score is kept for
    display and all hit sources are recorded in ``match_reasons``.
    """
    merged: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = {}

    for source, rows in ranked_lists.items():
        weight = SOURCE_WEIGHTS.get(source, 0.5)
        for rank, row in enumerate(rows or [], start=1):
            if not isinstance(row, dict):
                continue
            key = item_key(row)
            contribution = weight / (RRF_K + rank)
            scores[key] = scores.get(key, 0.0) + contribution
            existing = merged.get(key)
            if existing is None:
                item = dict(row)
                item["match_reasons"] = [source]
                item["score"] = row.get("score") or 0
                merged[key] = item
            else:
                if source not in existing["match_reasons"]:
                    existing["match_reasons"].append(source)
                if (row.get("score") or 0) > (existing.get("score") or 0):
                    existing["score"] = row.get("score") or 0

    for key, item in merged.items():
        item["fusion_score"] = scores.get(key, 0.0)
        reasons = item["match_reasons"]
        # Confidence is a display label, not a filter: two independent routes
        # agreeing is the only signal strong enough to call "high" on its own.
        if len(reasons) >= 2:
            item["confidence"] = "high"
        else:
            weight = SOURCE_WEIGHTS.get(reasons[0], 0.5)
            item["confidence"] = (
                "high" if weight >= 0.9 else "medium" if weight >= 0.7 else "low"
            )
        item["source"] = max(
            reasons,
            key=lambda name: SOURCE_WEIGHTS.get(name, 0.5),
        )

    return sorted(
        merged.values(),
        key=lambda item: (
            -float(item.get("fusion_score") or 0.0),
            -float(item.get("score") or 0.0),
            item_key(item),
        ),
    )


def apply_rerank(
    fused: list[dict[str, Any]],
    ordering: list[tuple[int, float]] | None,
) -> list[dict[str, Any]]:
    """Reorder fused candidates by a reranker result.

    ``ordering`` is ``[(index_in_fused, relevance_score), ...]``. Items the
    reranker did not return keep their fused order after the returned ones, so a
    truncated reranker response cannot drop evidence silently.
    """
    if not ordering:
        return fused
    seen: set[int] = set()
    ordered: list[dict[str, Any]] = []
    for index, score in ordering:
        if index < 0 or index >= len(fused) or index in seen:
            continue
        item = dict(fused[index])
        item["rerank_score"] = score
        ordered.append(item)
        seen.add(index)
    for index, item in enumerate(fused):
        if index not in seen:
            ordered.append(item)
    return ordered
