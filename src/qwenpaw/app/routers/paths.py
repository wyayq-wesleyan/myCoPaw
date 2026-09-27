# -*- coding: utf-8 -*-
"""Path management APIs.

Paths give one account a second isolation dimension: separate cases must not see
each other's material, while shared background (investigation techniques,
methodology) stays visible to several of them. Each path can hold knowledge spaces
and data tables, and is granted to the agents that may read it.

Two rules shape this API and are enforced in ``platform_store.paths`` rather than
here (so a direct call cannot bypass them):

* an agent with no grants sees nothing — access is explicit;
* a resource belongs to exactly one path — sharing happens by granting a common
  parent, never by duplicating a binding.

Every endpoint is scoped to the calling account, and both the listed paths and the
agents accepted for a grant are filtered by ownership, so one account cannot
inspect or attach another's resources.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ...platform_store import paths as path_store
from ..user_space import get_request_username, user_agent_ids
from ...config import load_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/paths", tags=["paths"])


class PathCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    parent_id: str | None = Field(
        default=None,
        description="父路径 id；省略或为 null 时挂到账号根路径下。",
    )


class PathRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class PathMoveRequest(BaseModel):
    parent_id: str | None = Field(
        default=None,
        description="新的父路径 id；null 表示移到账号根路径下。",
    )


class BindResourceRequest(BaseModel):
    resource_kind: str = Field(description="knowledge_space 或 data_table")
    resource_id: str = Field(min_length=1)


class BindAgentRequest(BaseModel):
    agent_id: str = Field(min_length=1)


def _username(request: Request) -> str:
    username = get_request_username(request)
    if not username:
        # Without an identity there is no account to scope to, and defaulting to
        # an account (admin in particular) would hand out its material.
        raise HTTPException(status_code=401, detail="无法确定当前账号。")
    return username


def _owned_agent_ids(username: str) -> set[str]:
    """Agent ids this account owns — the only ones it may attach a path to."""
    try:
        return set(user_agent_ids(load_config(), username))
    except Exception:  # noqa: BLE001 - config failure means no visible agents
        logger.warning("paths: could not resolve owned agents", exc_info=True)
        return set()


def _path_or_404(path_id: str, username: str) -> dict[str, Any]:
    node = path_store.path_by_id(path_id)
    if node is None or node["owner_username"] != username:
        # Same response for "missing" and "someone else's": distinguishing them
        # would confirm the existence of another account's paths.
        raise HTTPException(status_code=404, detail="路径不存在。")
    return node


def _handle_scope_error(exc: Exception) -> HTTPException:
    """Translate a domain error into a client error with its readable message."""
    return HTTPException(status_code=400, detail=str(exc))


@router.get("")
async def list_paths(request: Request) -> dict[str, Any]:
    """The account's whole path tree, with bound resources and agents.

    Returned as a tree because the UI presents it as one; flattening here would
    force the client to rebuild the hierarchy from an ordering convention.
    """
    username = _username(request)
    try:
        roots = path_store.list_path_tree(username)
    except Exception as exc:  # noqa: BLE001 - surface as a client error
        logger.warning("paths: listing failed", exc_info=True)
        raise HTTPException(status_code=503, detail=f"路径列表读取失败：{exc}") from exc
    return {"tree": [root.as_dict() for root in roots]}


@router.get("/tree")
async def path_tree_with_contents(request: Request) -> dict[str, Any]:
    """The path tree enriched with what each node holds and which agents see it.

    Separate from ``GET /paths`` because the extra facts are expensive: the
    knowledge space list comes from the knowledge service over HTTP, and the data
    table list from the registry. The plain tree endpoint stays cheap for callers
    that only need the structure (drag-and-drop, pickers).

    Each node carries:
      * ``document_count`` / ``table_count`` — what the node itself holds;
      * ``agents`` — the agents granted this node, so the UI can answer "who can
        see this case" without opening anything;
      * ``is_public`` — whether this is the account's public area (root), whose
        contents include everything not yet filed under a path. Surfaced
        explicitly because "unbound means public" is the one rule that is easy to
        misread as "already isolated".
    """
    username = _username(request)
    try:
        roots = path_store.list_path_tree(username)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"路径列表读取失败：{exc}") from exc

    # Best-effort enrichment: a failure here must not blank the tree, because the
    # structure is still useful (and the caller can retry).
    docs_by_space: dict[str, int] = {}
    space_ids: list[str] = []
    try:
        from .knowledge import _service_headers, _service_url
        import httpx

        url = _service_url()
        if url:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"{url}/v1/spaces", headers=_service_headers(username),
                )
                response.raise_for_status()
                spaces = response.json() or []
            space_ids = [str(item.get("id") or "") for item in spaces]
            for space_id in space_ids:
                if not space_id:
                    continue
                try:
                    async with httpx.AsyncClient(timeout=60) as client:
                        response = await client.get(
                            f"{url}/v1/spaces/{space_id}/documents",
                            headers=_service_headers(username),
                        )
                        response.raise_for_status()
                        docs_by_space[space_id] = len(response.json() or [])
                except Exception:  # noqa: BLE001 - one space must not blank the tree
                    logger.debug("paths: could not count docs in %s", space_id)
    except Exception:  # noqa: BLE001
        logger.warning("paths: knowledge enrichment unavailable", exc_info=True)

    table_ids: list[str] = []
    tables_by_id: dict[str, dict[str, Any]] = {}
    try:
        from ...data_tables.store import SYSTEM_TABLE_REGISTRY, _get_conn, _put_conn
        import psycopg2.extras

        conn = _get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"SELECT id, table_name, path_id FROM {SYSTEM_TABLE_REGISTRY} "
                    "WHERE owner_username = %s",
                    (username,),
                )
                for row in cur.fetchall():
                    table_ids.append(str(row["id"]))
                    tables_by_id[str(row["id"])] = dict(row)
        finally:
            _put_conn(conn)
    except Exception:  # noqa: BLE001
        logger.warning("paths: data table enrichment unavailable", exc_info=True)

    def decorate(node) -> dict[str, Any]:
        data = node.as_dict()
        visible_spaces = path_store.effective_space_ids_for_path(
            node.id, username, space_ids,
        ) or []
        visible_tables = path_store.effective_table_ids_for_path(
            node.id, username, table_ids,
        ) or []
        data["document_count"] = sum(
            docs_by_space.get(space_id, 0) for space_id in visible_spaces
        )
        data["table_count"] = len(visible_tables)
        data["space_count"] = len(visible_spaces)
        data["agents"] = sorted(path_store.agents_for_path(node.id))
        data["is_public"] = bool(node.is_root)
        data["children"] = [decorate(child) for child in node.children]
        return data

    return {"tree": [decorate(root) for root in roots]}


@router.get("/grants")
async def list_grants(request: Request) -> dict[str, Any]:
    """Which paths each of this account's agents may read.

    The mirror image of the tree view: an operator asking "what can this agent
    see" should not have to open every path.
    """
    username = _username(request)
    agents = sorted(_owned_agent_ids(username))
    result: dict[str, list[str]] = {}
    try:
        for agent_id in agents:
            result[agent_id] = sorted(path_store.agent_path_ids(agent_id))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"授权读取失败：{exc}") from exc
    return {"grants": result}


@router.post("", status_code=201)
async def create_path(request: Request, body: PathCreateRequest) -> dict[str, Any]:
    username = _username(request)
    try:
        path_id = path_store.create_path(
            username, body.name, parent_id=body.parent_id,
        )
    except path_store.PathScopeError as exc:
        raise _handle_scope_error(exc) from exc
    return {"id": path_id, "name": body.name, "parent_id": body.parent_id}


@router.patch("/{path_id}")
async def rename_path(
    request: Request, path_id: str, body: PathRenameRequest,
) -> dict[str, Any]:
    username = _username(request)
    _path_or_404(path_id, username)
    try:
        path_store.rename_path(path_id, body.name)
    except path_store.PathScopeError as exc:
        raise _handle_scope_error(exc) from exc
    return {"id": path_id, "name": body.name}


@router.post("/{path_id}/move")
async def move_path(
    request: Request, path_id: str, body: PathMoveRequest,
) -> dict[str, Any]:
    """Reparent a path (the drag-and-drop operation).

    Cycle rejection lives in the store: the UI cannot be trusted to prevent
    dropping a node into its own subtree, and a direct API call certainly cannot.
    """
    username = _username(request)
    _path_or_404(path_id, username)
    if body.parent_id is not None:
        _path_or_404(body.parent_id, username)
    try:
        path_store.move_path(path_id, body.parent_id)
    except path_store.PathScopeError as exc:
        raise _handle_scope_error(exc) from exc
    return {"id": path_id, "parent_id": body.parent_id}


@router.delete("/{path_id}")
async def delete_path(request: Request, path_id: str) -> dict[str, Any]:
    """Delete an empty path.

    Refused when the path still holds resources or children — deleting would
    cascade away the bindings and strand a case's material, so the caller has to
    move things out first. That makes the destructive step explicit.
    """
    username = _username(request)
    _path_or_404(path_id, username)
    try:
        path_store.delete_path(path_id)
    except path_store.PathScopeError as exc:
        raise _handle_scope_error(exc) from exc
    return {"deleted": True, "id": path_id}


# --- resource bindings -----------------------------------------------------


@router.get("/{path_id}/resources")
async def list_path_resources(request: Request, path_id: str) -> dict[str, Any]:
    username = _username(request)
    _path_or_404(path_id, username)
    items = path_store.resources_in_path(path_id)
    return {"path_id": path_id, "resources": items}


@router.put("/{path_id}/resources")
async def bind_resource(
    request: Request, path_id: str, body: BindResourceRequest,
) -> dict[str, Any]:
    """Attach a knowledge space or data table to a path.

    Binding *moves* the resource if it was in another path — the one-resource-one-
    path invariant is a primary key in the store, so this is the only way to
    reinterpret it.
    """
    username = _username(request)
    _path_or_404(path_id, username)
    try:
        path_store.bind_resource(path_id, body.resource_kind, body.resource_id)
    except path_store.PathScopeError as exc:
        raise _handle_scope_error(exc) from exc
    return {"path_id": path_id, "resource_id": body.resource_id}


@router.delete("/{path_id}/resources/{resource_kind}/{resource_id}")
async def unbind_resource(
    request: Request, path_id: str, resource_kind: str, resource_id: str,
) -> dict[str, Any]:
    """Detach a resource. It then falls back to the account's public (root) area.

    Note the consequence, which mirrors how unbound material is treated: detaching
    does **not** hide the resource — it becomes public again. Use a path with no
    agents granted to park material that should be reachable by nobody.
    """
    username = _username(request)
    _path_or_404(path_id, username)
    try:
        path_store.unbind_resource(resource_kind, resource_id)
    except path_store.PathScopeError as exc:
        raise _handle_scope_error(exc) from exc
    return {"unbound": True}


# --- agent grants ----------------------------------------------------------


@router.get("/{path_id}/agents")
async def list_path_agents(request: Request, path_id: str) -> dict[str, Any]:
    username = _username(request)
    _path_or_404(path_id, username)
    owned = _owned_agent_ids(username)
    # Only this account's agents: a path could otherwise show a stale grant to an
    # agent that has since been reassigned.
    agents = [a for a in path_store.agents_for_path(path_id) if a in owned]
    return {"path_id": path_id, "agents": agents}


@router.put("/{path_id}/agents")
async def bind_agent(
    request: Request, path_id: str, body: BindAgentRequest,
) -> dict[str, Any]:
    """Grant an agent read access to this path."""
    username = _username(request)
    _path_or_404(path_id, username)
    if body.agent_id not in _owned_agent_ids(username):
        raise HTTPException(
            status_code=403, detail="该智能体不属于当前账号。",
        )
    try:
        path_store.bind_agent(body.agent_id, path_id)
    except path_store.PathScopeError as exc:
        raise _handle_scope_error(exc) from exc
    return {"path_id": path_id, "agent_id": body.agent_id}


@router.delete("/{path_id}/agents/{agent_id}")
async def unbind_agent(
    request: Request, path_id: str, agent_id: str,
) -> dict[str, Any]:
    username = _username(request)
    _path_or_404(path_id, username)
    path_store.unbind_agent(agent_id, path_id)
    return {"unbound": True}
