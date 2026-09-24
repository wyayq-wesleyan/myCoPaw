# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,protected-access
"""ms.search() must never crash on raw user/model text.

The query is plain text, not a search-engine grammar: punctuation is a word
separator (so ``C++`` searches the term ``C``), whitespace-separated terms
within one bare-uppercase-``OR`` alternative are AND-combined, and matching is a
case-insensitive substring scan. That is what makes keyword recall work for
Chinese text, whose words are not whitespace-delimited — so a query like
``截止日期`` finds a sentence that contains it. These tests pin that contract.
"""

import pytest

from qwenpaw.agents.context.scroll.history import HistoryStore
from qwenpaw.agents.context.scroll.memoryspace import MemorySpace
from qwenpaw.agents.context.types import LogEntry


@pytest.fixture
def ms(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.append(
        session_id="s1",
        agent_id="ag1",
        dedup_key="m1",
        entry=LogEntry(
            kind="model_turn",
            role="assistant",
            content="discussing C++ and foo-bar templates",
            headline="cpp talk",
        ),
    )
    store.append(
        session_id="s1",
        agent_id="ag1",
        dedup_key="m2",
        entry=LogEntry(
            kind="model_turn",
            role="assistant",
            content="项目的截止日期是周二",
        ),
    )
    store.close()
    space = MemorySpace(
        history_db_path=str(store.path),
        session_id="s1",
        agent_id="ag1",
    )
    yield space
    space.close()


@pytest.mark.parametrize(
    "query",
    ["C++", "foo-bar", "what?", 'say "hi', "a OR b", "(unbalanced", "++"],
)
def test_search_never_raises_on_messy_query(ms, query):
    # Must return a list, not raise — that is the whole bug.
    assert isinstance(ms.search(query), list)


def test_search_finds_rows_for_plain_query(ms):
    rows = ms.search("templates")
    assert any("templates" in r["content"] for r in rows)


def test_search_treats_punctuation_as_a_separator(ms):
    """``C++`` searches the term ``C`` rather than the literal ``C++``."""
    contents = {r["content"] for r in ms.search("C++")}
    assert any("C++" in content for content in contents)


def test_search_matches_hyphenated_terms_by_either_half(ms):
    """``foo-bar`` splits into ``foo`` and ``bar``, AND-combined."""
    contents = {r["content"] for r in ms.search("foo-bar")}
    assert any("foo-bar" in content for content in contents)


def test_search_matches_cjk_substring(ms):
    """A CJK query is not whitespace-delimited, so it must match as a literal
    substring of the sentence that contains it."""
    contents = {r["content"] for r in ms.search("截止日期")}
    assert any("截止日期" in content for content in contents)


def test_search_uppercase_or_matches_either_term(ms):
    contents = {r["content"] for r in ms.search("templates OR 截止日期")}
    assert any("templates" in content for content in contents)
    assert any("截止日期" in content for content in contents)


def test_search_terms_within_one_arm_are_and_combined(ms):
    """Both terms must appear in ``content``: ``discussing templates`` matches,
    ``templates zzz`` does not."""
    assert ms.search("discussing templates")
    assert ms.search("templates zzz") == []


def test_search_like_metacharacters_are_literal(ms):
    """``%`` and ``_`` are data, not wildcards: they must not widen a search
    to every row."""
    assert ms.search("%") == []
    assert ms.search("_") == []
    # ``%template%`` would match by accident if the metacharacters were not
    # escaped; it must find nothing because the literal text is absent.
    assert ms.search("%templates%") == []
