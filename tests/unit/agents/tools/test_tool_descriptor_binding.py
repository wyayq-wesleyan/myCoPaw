# -*- coding: utf-8 -*-
"""A registered tool name must map to the tool's real implementation.

``@tool_descriptor`` registers the function object it decorates. When a helper
is refactored out of a tool and the decorator stays behind, the registry keeps
the *name* but points it at a private helper with an unrelated signature.

That is not a loud failure: the tool list still shows the name, so nothing looks
missing, but the agent is handed the wrong parameters and the call dies with
``ToolNotFoundError`` at answer time. It was hit in production on
``query_knowledge``, which had been registered onto ``_filter_search_payload``
(``(payload, allowed_document_ids)``) while the real ``query_knowledge``
(``(query, space_name)``) was never registered at all.

These tests pin the invariant for every built-in tool, so the next refactor
cannot silently re-point a name.
"""

from __future__ import annotations

import inspect

import pytest

import qwenpaw.agents.tools  # noqa: F401  (import triggers registration)
from qwenpaw.runtime.tool_registry import get_builtin_tool_funcs

# The knowledge tools the agent is told about in its system prompt. Each must
# resolve to a real, callable, correctly-shaped implementation.
KNOWLEDGE_TOOLS = (
    "query_knowledge",
    "get_document_detail",
    "create_knowledge_space",
    "upload_knowledge_document",
)


def _registered_by_name() -> dict:
    found = {}
    for fn in get_builtin_tool_funcs():
        descriptor = getattr(fn, "_tool_descriptor", None)
        if descriptor is not None:
            found[getattr(descriptor, "name", None)] = fn
    return found


def test_builtin_tools_are_registered() -> None:
    """Guards the rest of the module: an empty registry would pass vacuously."""
    assert len(_registered_by_name()) > 20


@pytest.mark.parametrize("tool_name", KNOWLEDGE_TOOLS)
def test_knowledge_tool_name_maps_to_its_own_function(tool_name: str) -> None:
    """The regression: the name must point at the function of that name."""
    registered = _registered_by_name().get(tool_name)

    assert registered is not None, f"{tool_name} is not registered"
    assert registered.__name__ == tool_name, (
        f"{tool_name} is registered onto {registered.__name__} — the decorator "
        "was left behind by a refactor and the real tool is unregistered"
    )


@pytest.mark.parametrize("tool_name", KNOWLEDGE_TOOLS)
def test_knowledge_tools_are_not_private_helpers(tool_name: str) -> None:
    """A leading underscore means an internal helper, never an agent tool."""
    registered = _registered_by_name().get(tool_name)

    assert registered is not None
    assert not registered.__name__.startswith("_")


def test_query_knowledge_exposes_the_parameters_the_agent_sends() -> None:
    """The agent sends ``{"query": …, "space_name": …}``; the schema must match.

    Registered onto the helper, the tool advertised ``payload`` /
    ``allowed_document_ids`` instead — unusable, and the reason the call failed
    even though the tool appeared in the list.
    """
    fn = _registered_by_name()["query_knowledge"]

    params = list(inspect.signature(fn).parameters)
    assert params[:2] == ["query", "space_name"]
    assert inspect.iscoroutinefunction(fn), (
        "query_knowledge does network I/O and must stay async"
    )


def test_no_tool_descriptor_sits_on_a_private_function() -> None:
    """Applies to every built-in tool, not just the knowledge ones.

    Catches the whole class: any decorator left on a ``_``-prefixed helper
    during a refactor.
    """
    offenders = [
        (fn.__name__, getattr(fn, "_tool_descriptor").name)
        for fn in get_builtin_tool_funcs()
        if fn.__name__.startswith("_")
    ]

    assert offenders == [], (
        "these tool names are registered onto private helpers: "
        f"{offenders}"
    )


def test_registered_tool_name_matches_function_name() -> None:
    """A mismatch means the descriptor was moved onto the wrong function."""
    mismatches = [
        (getattr(fn, "_tool_descriptor").name, fn.__name__)
        for fn in get_builtin_tool_funcs()
        if getattr(fn, "_tool_descriptor").name != fn.__name__
    ]

    assert mismatches == [], (
        f"registered name does not match the implementation: {mismatches}"
    )
