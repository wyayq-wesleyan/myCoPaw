# -*- coding: utf-8 -*-
"""Structured references attached to knowledge/data tool results."""

from __future__ import annotations

from qwenpaw.agents.tools import data_tables, knowledge


def test_sql_references_include_from_and_join_tables():
    refs = data_tables._references_from_sql(
        "SELECT * FROM tb_scores s JOIN tb_people p ON true",
    )
    assert refs == [
        {"kind": "data_table", "name": "tb_scores"},
        {"kind": "data_table", "name": "tb_people"},
    ]


def test_sql_references_skip_cte_names_and_duplicates():
    refs = data_tables._references_from_sql(
        "WITH recent AS (SELECT * FROM tb_scores) "
        "SELECT * FROM recent JOIN tb_scores ON true",
    )
    assert refs == [{"kind": "data_table", "name": "tb_scores"}]


def test_knowledge_references_cover_every_document_not_just_the_first_five():
    targets = [
        (
            "默认",
            {
                "documents": [
                    {"document_id": f"doc-{index}", "filename": f"文件{index}.pdf"}
                    for index in range(7)
                ],
            },
        ),
    ]
    refs = knowledge._knowledge_references(targets)
    assert len(refs) == 7
    assert refs[0] == {
        "kind": "knowledge",
        "id": "doc-0",
        "name": "文件0.pdf",
        "space": "默认",
    }


def test_knowledge_references_are_deduplicated_across_spaces():
    targets = [
        ("空间A", {"documents": [{"document_id": "doc-1", "filename": "a.pdf"}]}),
        ("空间B", {"documents": [{"document_id": "doc-1", "filename": "a.pdf"}]}),
    ]
    assert knowledge._knowledge_references(targets) == [
        {"kind": "knowledge", "id": "doc-1", "name": "a.pdf", "space": "空间A"},
    ]
