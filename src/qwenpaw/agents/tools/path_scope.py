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

# The account's root path maps to the shared ``public`` schema — which is where
# every pre-existing table lives, so the upgrade moves nothing. Kept in sync with
# ``data_tables.store.ROOT_SCHEMA`` (imported lazily there to avoid a cycle).
ROOT_SCHEMA = "public"


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


def data_search_path(owner_username: str) -> list[str] | None:
    """The ``search_path`` an agent's data queries should run under, or ``None``.

    Returned as an ordered list because an agent legitimately spans several paths
    — the account's public (root) area plus the cases it is working on. PostgreSQL
    resolves an unqualified table name against the first schema in
    ``search_path`` that has it, so a list expresses "my case first, then the
    shared material" directly, and no single-schema choice has to be made.

    Order matters and is deliberate: **the agent's granted non-root paths come
    first, ``public`` (root) last.** A case-specific table therefore shadows a
    same-named table in the public area, which is the intuitive precedence —
    material filed under a case is more specific than shared material.

    ``None`` means "deny": either the identity is unknown, or the agent has no
    grants at all. Callers must treat it as no access, never as "unscoped access".
    """
    agent_id = current_agent_id()
    if not agent_id:
        return None
    try:
        from ...platform_store import paths

        granted = paths.agent_path_ids(agent_id)
        if not granted:
            # fail-closed: an agent with no grants sees no tables at all.
            return None
        schemas: list[str] = []
        for path_id in granted:
            node = paths.path_by_id(path_id)
            if node is None:
                continue
            # Root maps to the shared ``public`` schema; every other path gets its
            # own UUID-derived schema.
            schemas.append(
                ROOT_SCHEMA if node.get("is_root") else paths.data_schema_for(path_id)
            )
    except Exception:  # noqa: BLE001 - store failure must deny, not allow
        logger.warning(
            "path_scope: refusing data access — path store unavailable",
            exc_info=True,
        )
        return None

    if not schemas:
        return None
    # De-duplicate while preserving order (an agent granted the same path twice is
    # impossible via the primary key, but the root may also arrive as a child).
    ordered = list(dict.fromkeys(schemas))
    # ``public`` last so a case's table shadows a same-named public one. If the
    # agent was granted only the root, this is simply ["public"].
    if ROOT_SCHEMA in ordered and len(ordered) > 1:
        ordered = [s for s in ordered if s != ROOT_SCHEMA] + [ROOT_SCHEMA]
    return ordered


def visible_data_path_ids(owner_username: str) -> set[str] | None:
    """Path ids whose data tables this agent may list, or ``None`` to deny.

    Used by the registry-driven listings (which read metadata from the system
    database rather than the account database), so they show the same set the
    query tool can actually reach — otherwise the list would advertise tables the
    agent then cannot query, or hide ones it can.

    A registry row with an empty ``path_id`` is an unbound (pre-existing) table;
    callers normalise it to the root path id, matching
    ``paths.effective_path_of``. ``None`` means deny (unknown identity or no
    grants), and must not be read as "no filter".
    """
    agent_id = current_agent_id()
    if not agent_id:
        return None
    try:
        from ...platform_store import paths

        granted = paths.agent_path_ids(agent_id)
        if not granted:
            return None
        return set(granted)
    except Exception:  # noqa: BLE001 - deny on store failure
        logger.warning(
            "path_scope: refusing data listing — path store unavailable",
            exc_info=True,
        )
        return None


def root_path_id(owner_username: str) -> str | None:
    """The account's root path id, or ``None`` if it cannot be resolved."""
    try:
        from ...platform_store import paths

        return paths.ensure_root_path(owner_username)
    except Exception:  # noqa: BLE001
        logger.warning("path_scope: could not resolve the root path", exc_info=True)
        return None


__all__ = [
    "ROOT_SCHEMA",
    "current_agent_id",
    "data_search_path",
    "root_path_id",
    "visible_data_path_ids",
    "visible_space_ids",
]
