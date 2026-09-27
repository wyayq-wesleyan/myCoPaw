# -*- coding: utf-8 -*-
"""One place that answers "what may this agent reach?".

Every enforcement point (the knowledge tool, the data-table tools, the console
listings) must apply the same rule. Reimplementing it per tool is how isolation
silently breaks: a caller that forgets the unbound-resource root fallback hides
public material, and one that forgets the grant check exposes a case.

The rule itself lives in :mod:`qwenpaw.platform_store.paths`; this module only
resolves *who is asking* and adapts failures into "deny", never "allow":

* If the platform database is unreachable, the caller is denied. Failing open
  here would mean an outage removes every isolation boundary at once.
* If the agent identity cannot be determined, the caller is denied rather than
  treated as the administrator (the same direction as
  ``resolve_account_username``, and for the same reason).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def current_agent_id() -> str:
    """The agent executing this tool call, or ``""`` when unknown."""
    try:
        from ...app.agent_context import get_current_agent_id

        agent = get_current_agent_id()
    except Exception:  # noqa: BLE001 - unknown identity must not raise
        logger.debug("path_scope: could not resolve agent id", exc_info=True)
        return ""
    if not agent or agent == "default":
        # ``"default"`` is the placeholder the request hook writes when there is
        # no real agent, and the deployment happens to contain an agent literally
        # named ``default`` owned by the administrator — resolving the placeholder
        # would turn "identity unknown" into "act as admin".
        return ""
    return str(agent)


def visible_space_ids(owner_username: str, space_ids: list[str]) -> list[str]:
    """Filter knowledge space ids down to those this agent may read.

    ``space_ids`` is what the account owns (already owner-scoped by the knowledge
    service); this narrows it to the agent's granted paths. An empty result means
    the agent has no grants — the caller must present that as "no accessible
    material", not as "the search found nothing".
    """
    if not space_ids:
        return []
    agent_id = current_agent_id()
    if not agent_id:
        logger.warning(
            "path_scope: refusing knowledge access — agent identity unknown "
            "(owner=%s)",
            owner_username,
        )
        return []
    try:
        from ...platform_store import paths

        # Granted *path* ids. Comparing them against resource ids would never
        # match (the two are different namespaces) and would silently filter out
        # everything the agent should see.
        allowed = set(paths.agent_path_ids(agent_id))
        root = paths.ensure_root_path(owner_username)
    except Exception:  # noqa: BLE001 - unavailable store must deny, not allow
        logger.warning(
            "path_scope: refusing knowledge access — path store unavailable",
            exc_info=True,
        )
        return []

    # A space with no binding is public (lives in the root path); one with a
    # binding is visible only if that path was granted.
    visible: list[str] = []
    for space_id in space_ids:
        try:
            bound = paths.path_of_resource(paths.KIND_KNOWLEDGE_SPACE, space_id)
            effective = bound if bound is not None else root
        except Exception:  # noqa: BLE001 - deny this one rather than leak it
            logger.warning(
                "path_scope: could not resolve path for space %s", space_id,
                exc_info=True,
            )
            continue
        if effective in allowed:
            visible.append(space_id)
    return visible


def data_schema_for_agent(owner_username: str) -> str | None:
    """The PostgreSQL schema holding this agent's data tables, or ``None``.

    Unlike knowledge (many spaces filtered one by one), a data table is a plain
    relation inside a schema, and the isolation is physical: the connection's
    ``search_path`` points at one schema, so a query cannot name another path's
    table at all. That means an agent must resolve to **exactly one** schema here.

    An agent granted several paths is therefore not yet supported: silently
    picking one would hide the others' tables with no explanation. Returning
    ``None`` plus a log line makes the situation explicit instead.
    """
    agent_id = current_agent_id()
    if not agent_id:
        return None
    try:
        from ...platform_store import paths

        allowed = paths.agent_path_ids(agent_id)
    except Exception:  # noqa: BLE001 - deny on store failure
        logger.warning(
            "path_scope: refusing data access — path store unavailable",
            exc_info=True,
        )
        return None

    if not allowed:
        # No grants at all: fail closed. (The root being "public" is expressed by
        # the upgrade granting it to every agent, not by waving this check.)
        return None
    if len(allowed) == 1:
        return paths.data_schema_for(allowed[0])
    # Several paths granted: see the docstring — not silently resolved.
    logger.warning(
        "path_scope: agent %s is granted %d paths; data access needs exactly one "
        "(granted=%s)",
        agent_id,
        len(allowed),
        sorted(allowed),
    )
    return None


__all__ = [
    "current_agent_id",
    "data_schema_for_agent",
    "visible_space_ids",
]
