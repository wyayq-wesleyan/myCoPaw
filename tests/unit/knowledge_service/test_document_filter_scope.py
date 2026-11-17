# -*- coding: utf-8 -*-
"""Document-level reachability must be enforced inside retrieval.

The path model lives in the main app, so the knowledge service receives an
explicit document allow-list. It must apply that list in SQL for **every**
retrieval route. Two failure modes are guarded here:

* a restricted caller receives a document it was not granted (leak);
* a restricted caller loses its own hits because other documents consumed the
  top_k slots (the "case A search returns nothing although the document is
  there" bug).
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import sys
import uuid

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
STORE_PATH = REPO_ROOT / "knowledge-service" / "app" / "store.py"
MAIN_PATH = REPO_ROOT / "knowledge-service" / "app" / "main.py"

PG_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL", "")

try:
    import psycopg  # noqa: F401

    _HAS_PSYCOPG = True
except ImportError:  # pragma: no cover - main-app venv may not ship psycopg
    _HAS_PSYCOPG = False


def _method_body(path: pathlib.Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    match = re.search(rf"\n    def {name}\(", source)
    assert match, f"{name} not found in {path}"
    tail = source[match.end():]
    end = re.search(r"\n    (?:def |@)", tail)
    return tail[: end.start()] if end else tail


def test_store_has_one_document_filter_helper():
    source = STORE_PATH.read_text(encoding="utf-8")
    assert "def document_filter(" in source
    # Empty allow-list must mean deny, never "no filter".
    body = source[source.index("def document_filter("):]
    assert "if not document_ids:" in body[:1200]
    assert "AND FALSE" in body[:1200]


@pytest.mark.parametrize(
    "method",
    [
        "search",
        "search_by_filename",
        "entity_context",
        "relation_context",
    ],
)
def test_every_retrieval_route_applies_the_filter(method):
    body = _method_body(STORE_PATH, method)
    assert "document_ids" in body, f"{method} must accept document_ids"
    assert "document_filter(" in body, f"{method} must apply the SQL filter"


def test_embedding_search_delegates_the_filter_to_its_sql_helpers():
    body = _method_body(STORE_PATH, "search_by_embedding")
    assert "document_ids" in body
    assert "_vector_search_sql(" in body and "document_ids" in body
    assert "_load_all_vectors(db, space_id, document_ids)" in body


def test_hybrid_search_fails_closed_on_an_empty_allow_list():
    main = MAIN_PATH.read_text(encoding="utf-8")
    assert "document_ids" in main
    assert "if document_ids is not None and not document_ids:" in main
    assert "return [], [], False" in main


def test_graph_is_skipped_when_document_scope_cannot_be_applied():
    """Graph rows carry no document id, so they may only be used with workspaces."""

    main = MAIN_PATH.read_text(encoding="utf-8")
    assert "allow_graph = document_ids is None or bool(workspaces)" in main


# --------------------------------------------------------------------------
# Real PostgreSQL behaviour
# --------------------------------------------------------------------------


def _load_store():
    spec = importlib.util.spec_from_file_location(
        "ks_store_for_filter_test", STORE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def store():
    if not PG_URL or not _HAS_PSYCOPG:
        pytest.skip("set KNOWLEDGE_TEST_DATABASE_URL to run the SQL checks")
    module = _load_store()
    return module.Store(PG_URL)


def _seed_document(store, space_id: str, owner: str, filename: str, text: str) -> str:
    document_id = str(uuid.uuid4())
    store.create_document(
        space_id,
        owner,
        filename,
        "/tmp/" + filename,
        "text/plain",
        document_id=document_id,
    )
    document = {
        "id": document_id,
        "space_id": space_id,
        "filename": filename,
    }
    store.replace_document_content(
        document,
        chunks=[
            {
                "page": 1,
                "location": "正文",
                "content": text,
                "source_type": "text",
            }
        ],
        extracted={"entities": [], "relations": []},
        embeddings=None,
    )
    return document_id


def test_filtered_search_returns_only_allowed_documents(store):
    owner = "e2e-filter-" + uuid.uuid4().hex[:8]
    space_id = str(store.create_space("filter", owner)["id"])
    doc_a = _seed_document(store, space_id, owner, "案件A报告.txt", "测试关键词 案件甲 内容")
    doc_b = _seed_document(store, space_id, owner, "案件B报告.txt", "测试关键词 案件乙 内容")

    all_hits = store.search(space_id, "测试关键词", 10)
    assert {hit["document_id"] for hit in all_hits} == {doc_a, doc_b}

    only_a = store.search(space_id, "测试关键词", 10, [doc_a])
    assert only_a
    assert {hit["document_id"] for hit in only_a} == {doc_a}

    only_b = store.search_by_filename(space_id, "报告", 10, [doc_b])
    assert {hit["document_id"] for hit in only_b} == {doc_b}

    assert store.search(space_id, "测试关键词", 10, []) == []
    assert store.entity_context(space_id, "案件", 10, []) == []
    assert store.relation_context(space_id, "案件", 10, []) == []

    # Leave the shared test database as we found it: other tests in this suite
    # assume they may skip when there are no chunks, and a leftover 8-dim or
    # unembedded row changes that decision.
    with store._connect() as db:
        db.execute("DELETE FROM jobs WHERE document_id = ANY(%s)", ([doc_a, doc_b],))
        db.execute("DELETE FROM chunks WHERE space_id = %s", (space_id,))
        db.execute("DELETE FROM documents WHERE space_id = %s", (space_id,))
        db.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
