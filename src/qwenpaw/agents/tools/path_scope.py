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


def visible_document_ids(
    owner_username: str,
    all_document_ids: list[str],
) -> list[str]:
    """Which of the account's documents *this agent* may read. Empty = none.

    Knowledge is filed per document, so the allowed set is the union over the
    agent's granted paths of the documents filed there, plus — when the agent
    holds the root path — every unfiled document (the account's public area).

    ``all_document_ids`` is the account's full list, supplied by the caller
    because the documents live in the knowledge service. It is *required* for the
    root case: without it, "unfiled" cannot be told apart from "filed elsewhere",
    and a root-only agent would see nothing.
    """
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

        granted = set(paths.agent_path_ids(agent_id))
        root = paths.ensure_root_path(owner_username)
    except Exception:  # noqa: BLE001 - unavailable store must deny, not allow
        logger.warning(
            "path_scope: refusing knowledge access — path store unavailable",
            exc_info=True,
        )
        return []

    if not granted:
        # fail-closed: an agent with no grants reads nothing.
        return []

    allowed: set[str] = set()
    try:
        for path_id in granted:
            visible = paths.effective_document_ids_for_path(
                path_id, owner_username, all_document_ids,
            )
            if visible:
                allowed.update(visible)
    except Exception:  # noqa: BLE001 - deny rather than leak on a partial failure
        logger.warning(
            "path_scope: refusing knowledge access — document lookup failed",
            exc_info=True,
        )
        return []

    return sorted(allowed)


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


# ---------------------------------------------------------------------------
# Resolving a path the *user* named (a case name, or an id)
# ---------------------------------------------------------------------------


class PathNotWritable(ValueError):
    """The named path cannot be written to by this agent."""


class PathAmbiguous(ValueError):
    """The name matches more than one granted path; the caller must disambiguate."""

    def __init__(self, candidates: list[dict[str, str]]) -> None:
        self.candidates = candidates
        # Include the ids: when the collision is two paths with the *same* name,
        # repeating the name (the earlier form) gives the reader nothing to choose
        # between, and the id is the only distinguishing value there is.
        described = "、".join(
            f"{c.get('name') or '未命名'}（{c.get('id') or '无 id'}）"
            for c in candidates
        )
        super().__init__(
            f"「{candidates[0].get('name') if candidates else ''}」"
            f"匹配到多个路径：{described}。请指明要用哪一个（可用路径 id）。",
        )


def granted_path_nodes(owner_username: str) -> list[dict[str, str]] | None:
    """Paths this agent may write into, as ``{id, name, is_root, parent_id}``.

    ``None`` means deny (unknown identity, no grants, or an unreachable store) —
    the same fail-closed rule the read paths use. An empty list can never be
    returned: no grants is ``None``, so callers cannot mistake "nothing" for
    "unfiltered".
    """
    agent_id = current_agent_id()
    if not agent_id:
        return None
    try:
        from ...platform_store import paths as path_store

        granted = path_store.agent_path_ids(agent_id)
        if not granted:
            return None
        nodes: list[dict[str, str]] = []
        for path_id in granted:
            node = path_store.path_by_id(path_id)
            if not node:
                # A grant pointing at a deleted path grants nothing; skip rather
                # than fail the whole call, so the other cases stay usable.
                continue
            nodes.append(
                {
                    "id": str(node.get("id") or ""),
                    "name": str(node.get("name") or ""),
                    "is_root": bool(node.get("is_root")),
                    "parent_id": str(node.get("parent_id") or ""),
                },
            )
        return nodes
    except Exception:  # noqa: BLE001 - deny rather than widen on failure
        logger.warning(
            "path_scope: could not resolve granted paths", exc_info=True,
        )
        return None


def resolve_writable_path_id(owner_username: str, reference: str) -> str:
    """Turn what the user said (a case name or an id) into a writable path id.

    Returning ``""`` means the account's public area (the root path), which is
    where resources go when no case is named — the pre-existing behaviour.

    **Only granted paths are candidates.** Letting the model file material into a
    case the agent was never granted would be a write-side isolation hole: the
    agent could not read that case, but it could still add to it, and the case's
    own agent would then see material appear from nowhere. Names are matched only
    inside the granted set, so an un-granted case name simply does not resolve.

    Raises :class:`PathNotWritable` when nothing matches and
    :class:`PathAmbiguous` when several do — a wrong guess here silently misfiles
    the user's data, so ambiguity is reported instead of resolved.
    """
    reference = (reference or "").strip()
    if not reference:
        return ""

    nodes = granted_path_nodes(owner_username)
    if nodes is None:
        raise PathNotWritable(
            "当前智能体没有可写入的路径。请先在「数据分析」中为该智能体挂载路径。",
        )

    # An explicit id always wins: it is unambiguous by construction.
    for node in nodes:
        if node["id"] == reference:
            return "" if node["is_root"] else node["id"]

    exact = [node for node in nodes if node["name"] == reference]
    if len(exact) == 1:
        node = exact[0]
        return "" if node["is_root"] else node["id"]
    if len(exact) > 1:
        raise PathAmbiguous(exact)

    # Fall back to a case-insensitive / substring match so "销售案件" finds
    # "2024销售案件". Ambiguity is still reported rather than guessed.
    lowered = reference.lower()
    loose = [
        node
        for node in nodes
        if lowered in node["name"].lower() and not node["is_root"]
    ]
    if len(loose) == 1:
        return loose[0]["id"]
    if len(loose) > 1:
        raise PathAmbiguous(loose)

    visible = "、".join(
        f"{node['name']}（默认区）" if node["is_root"] else node["name"]
        for node in nodes
    )
    raise PathNotWritable(
        f"没有找到名为「{reference}」的可写入路径。当前可写入：{visible}。"
        f"如需新案件，请先在「数据分析」中创建并挂载给该智能体。",
    )


__all__ = [
    "ROOT_SCHEMA",
    "PathAmbiguous",
    "PathNotWritable",
    "current_agent_id",
    "data_search_path",
    "granted_path_nodes",
    "resolve_writable_path_id",
    "root_path_id",
    "visible_data_path_ids",
    "visible_document_ids",
]
