"""Filename-only information must be retrievable.

Measured on real data: none of the content indexes can see filenames.
``search_vector`` is generated from ``content`` alone (see the ``chunks`` DDL),
the bigram literal path matches ``content``, the embeddings embed ``content``,
and entities are extracted from ``content``. A query naming a token that exists
*only* in a filename — version tags like ``_上会版``, student/ticket ids,
revision years — therefore returned nothing, even when a document was named
exactly that. On the deployment's own corpus, a term present only in a filename
scored 0.0 recall while a term present in the body scored 1.0.

Two layers of checking:

* static guards below run everywhere and fail if the filename path is dropped
  from the SQL, from the fusion, or from the source weights;
* ``test_filename_only_term_is_recalled_from_postgres`` exercises the real
  query against PostgreSQL. It is skipped unless ``KNOWLEDGE_TEST_DATABASE_URL``
  is set and ``psycopg`` is importable (the knowledge-service venv), because the
  behaviour is a SQL property a static assertion cannot confirm.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
STORE_PATH = REPO_ROOT / "knowledge-service" / "app" / "store.py"
MAIN_PATH = REPO_ROOT / "knowledge-service" / "app" / "main.py"

try:  # only present in the knowledge-service environment
    import psycopg  # noqa: F401

    _HAS_PSYCOPG = True
except ImportError:  # pragma: no cover - exercised by the default test env
    _HAS_PSYCOPG = False

PG_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL", "")


def _store_source() -> str:
    return STORE_PATH.read_text(encoding="utf-8")


def _method_body(name: str) -> str:
    """Return the source of one ``Store`` method, up to the next def."""
    source = _store_source()
    match = re.search(rf"\n    def {name}\(", source)
    assert match, f"Store.{name} not found in {STORE_PATH}"
    tail = source[match.end():]
    end = re.search(r"\n    (?:def |@)", tail)
    return tail[: end.start()] if end else tail


def _main_source() -> str:
    return MAIN_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Static guards
# --------------------------------------------------------------------------


def test_filename_search_exists_and_matches_the_filename_column():
    body = _method_body("search_by_filename")

    assert "d.filename" in body or "doc.filename" in body, (
        "search_by_filename must match the filename column"
    )
    assert "LIKE" in body
    # It must not fall back to content matching: that is the path that already
    # exists and is exactly what could not see filenames.
    assert "c.content LIKE" not in body, (
        "search_by_filename must not scan chunk content"
    )


def test_filename_search_returns_the_opening_chunk():
    """The model should see what the document is, not an arbitrary slice."""
    body = _method_body("search_by_filename")

    assert "min(" in body and "sequence" in body, (
        "filename hits must pick the document's first chunk"
    )


def test_filename_search_is_scoped_to_the_space():
    body = _method_body("search_by_filename")

    assert "space_id" in body, "filename search must stay inside one space"


def test_hybrid_search_fuses_filename_hits():
    main = _main_source()

    assert "search_by_filename" in main, (
        "_hybrid_search must call the filename path"
    )
    # The results have to enter the merge list, or the call is inert.
    merge = re.search(
        r"for item in \[(.*?)\]:", main, re.S,
    )
    assert merge, "could not locate the fusion loop in _hybrid_search"
    assert "by_filename" in merge.group(1), (
        "filename hits must be part of the fused candidate list"
    )


def test_filename_hits_are_merged_before_content_hits():
    """``merged`` is insertion-ordered and truncated to top_k.

    Appending filename hits at the end would let content hits push them out of
    the result — defeating the case where the query term is only in a filename.
    """
    merge = re.search(r"for item in \[(.*?)\]:", _main_source(), re.S)
    assert merge, "could not locate the fusion loop in _hybrid_search"
    order = [part.strip() for part in merge.group(1).split(",") if part.strip()]

    assert order and order[0] == "*by_filename", (
        f"filename hits must be merged first, got order {order}"
    )


def test_filename_source_has_a_weight():
    weights = re.search(r"SOURCE_WEIGHT = \{(.*?)\}", _main_source(), re.S)
    assert weights, "SOURCE_WEIGHT not found in _hybrid_search"
    assert '"filename"' in weights.group(1), (
        "the filename source needs a weight, or it falls back to 0.5 and is "
        "reported as low confidence"
    )


def test_filename_source_is_labelled_for_the_model():
    for path, allow_shorthand in (
        (REPO_ROOT / "src/qwenpaw/agents/react_agent.py", True),
        (REPO_ROOT / "src/qwenpaw/agents/tools/knowledge.py", False),
    ):
        text = path.read_text(encoding="utf-8")
        assert '"filename"' in text, f"{path} is missing the filename label"
        assert "文件名匹配" in text, f"{path} is missing the Chinese label"


# --------------------------------------------------------------------------
# Real PostgreSQL behaviour
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not (_HAS_PSYCOPG and PG_URL),
    reason="set KNOWLEDGE_TEST_DATABASE_URL and install psycopg to run",
)
def test_filename_only_term_is_recalled_from_postgres():
    """The decisive case: a token that exists only in a filename."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "knowledge-service"))
    from app.store import Store  # noqa: PLC0415

    store = Store(PG_URL)
    space_id = f"itest-{uuid.uuid4().hex}"
    token = f"XYZ{uuid.uuid4().hex[:8]}"  # never appears in any body text
    named_doc, other_doc = f"doc-{uuid.uuid4().hex}", f"doc-{uuid.uuid4().hex}"

    try:
        with store._connect() as db:  # noqa: SLF001
            db.execute(
                "INSERT INTO spaces (id, name, owner_id, created_at)"
                " VALUES (%s, %s, %s, now())",
                (space_id, "itest", "itest-owner"),
            )
            for doc_id, filename in (
                (named_doc, f"季度报告_{token}_上会版.md"),
                (other_doc, "无关文档.md"),
            ):
                db.execute(
                    "INSERT INTO documents"
                    " (id, space_id, owner_id, filename, path, status,"
                    "  created_at, updated_at)"
                    " VALUES (%s, %s, %s, %s, %s, 'completed', now(), now())",
                    (doc_id, space_id, "itest-owner", filename, f"/tmp/{doc_id}"),
                )
            db.execute(
                "INSERT INTO chunks (id, document_id, space_id, sequence, page,"
                " location, content) VALUES (%s, %s, %s, 0, 1, 'p1', %s)",
                (f"c-{uuid.uuid4().hex}", named_doc, space_id, "正文里没有这个编号"),
            )
            db.execute(
                "INSERT INTO chunks (id, document_id, space_id, sequence, page,"
                " location, content) VALUES (%s, %s, %s, 0, 1, 'p1', %s)",
                (f"c-{uuid.uuid4().hex}", other_doc, space_id, "毫不相干的内容"),
            )

        # The content paths cannot see the token, which is the pre-fix failure.
        assert not store.search(space_id, token, 10), (
            "precondition: the token must not appear in any chunk content"
        )

        hits = store.search_by_filename(space_id, token, 10)

        assert hits, "a term matching only a filename must still be recalled"
        assert all(hit["filename"].startswith("季度报告_") for hit in hits)
        assert all(hit["source"] == "filename" for hit in hits)
        assert hits[0]["document_id"] == named_doc
    finally:
        # documents reference spaces without ON DELETE CASCADE, so remove them
        # first; chunks then go with their documents.
        with store._connect() as db:  # noqa: SLF001
            db.execute(
                "DELETE FROM documents WHERE id IN (%s, %s)",
                (named_doc, other_doc),
            )
            db.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
