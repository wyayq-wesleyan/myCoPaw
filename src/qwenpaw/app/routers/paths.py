# -*- coding: utf-8 -*-
"""Path management APIs.

Paths give one account a second isolation dimension: separate cases must not see
each other's material, while shared background (investigation techniques,
methodology) stays visible to several of them. Each path can hold knowledge
**documents** and data tables, and is granted to the agents that may read it.

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

# The account a single-account deployment (auth disabled) is treated as. The
# codebase already uses it as that default: its workspace is ``default`` and
# unowned agents are assigned to it.
_SINGLE_ACCOUNT = "admin"

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
    resource_kind: str = Field(description="knowledge_document 或 data_table")
    resource_id: str = Field(min_length=1)


class BindAgentRequest(BaseModel):
    agent_id: str = Field(min_length=1)


def _username(request: Request) -> str:
    """The account whose paths this request may see or change.

    With authentication **enabled**, an unresolvable identity is a hard 401:
    scoping the request to a guessed account would expose that account's paths.

    With authentication **disabled** (single-account deployments), there is no
    account dimension to resolve, so the deployment's own account is used. It must
    be a concrete name rather than empty — paths are stored per owner, so an empty
    owner would resolve to an empty tree and make the feature look broken. Note
    this is deliberately *not* a 401: the console's 401 interceptor clears the
    session and redirects to the login page, so answering 401 on a deployment that
    has no login would eject the user from the app.
    """
    username = get_request_username(request)
    if username:
        return username
    if _auth_enabled():
        raise HTTPException(status_code=401, detail="无法确定当前账号。")
    # No account dimension: this deployment's own account is what the paths belong
    # to. Returning a concrete name (rather than "") matters because paths are
    # stored per owner, so an empty owner would resolve to an empty tree and make
    # the feature look broken on single-account installs.
    return _SINGLE_ACCOUNT


def _auth_enabled() -> bool:
    """Whether this deployment separates accounts; unknown counts as enabled."""
    try:
        from ..auth import is_auth_enabled

        return bool(is_auth_enabled())
    except Exception:  # noqa: BLE001 - unreadable flag must not widen access
        return True





def _ensure_account_root(username: str) -> None:
    """Create the account's root path (public area) if it is missing.

    **Every account must have a tree with at least the public area**, and this is
    where that invariant is enforced on the read path. Relying on the startup
    migration alone was not enough: it only walks accounts that already own an
    agent, and it runs once per boot — so an account created (or an agent whose
    profile appeared) while the app was running showed an **empty** path tree on
    both pages until the next restart.

    Creating the root changes no existing data: unbound resources already belong to
    it, so this only makes an implicit bucket visible. It deliberately does **not**
    grant the root to any agent — a user may have unchecked the public area in the
    chat header on purpose, and silently re-granting it would undo that choice.
    """
    try:
        path_store.ensure_root_path(username)
    except Exception:  # noqa: BLE001 - the tree still renders, just without a root
        logger.warning(
            "paths: could not ensure a root path for %s", username, exc_info=True,
        )


def _owned_agent_ids(username: str) -> set[str]:
    """Agent ids this account owns — the only ones it may attach a path to.

    ``username`` empty means "no account dimension" (auth disabled); the shared
    ``user_agent_ids`` already returns every profile in that case, which is the
    convention the rest of the console follows.
    """
    try:
        return set(user_agent_ids(load_config(), username or None))
    except Exception:  # noqa: BLE001 - config failure means no visible agents
        logger.warning("paths: could not resolve owned agents", exc_info=True)
        return set()


def _path_or_404(path_id: str, username: str) -> dict[str, Any]:
    node = path_store.path_by_id(path_id)
    if node is None:
        raise HTTPException(status_code=404, detail="路径不存在。")
    if username and node["owner_username"] != username:
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
    _ensure_account_root(username)
    try:
        roots = path_store.list_path_tree(username)
    except Exception as exc:  # noqa: BLE001 - surface as a client error
        logger.warning("paths: listing failed", exc_info=True)
        raise HTTPException(status_code=503, detail=f"路径列表读取失败：{exc}") from exc
    return {"tree": [root.as_dict() for root in roots]}


def _human_size(size: Any) -> str:
    """A file size the picker can show next to a filename."""
    try:
        value = float(size or 0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


async def _account_document_ids(username: str) -> list[str]:
    """Every document id this account owns, across all pages.

    Paginated deliberately: ``/v1/documents`` caps ``page_size`` at 100, and a
    truncated list would make the root path undercount its public material (the
    unbound set is derived from this list). Under-counting is the safe direction —
    it can never expose a filed document — but it would still make the tree's
    numbers wrong, so the whole list is read.
    """
    from .knowledge import _service_headers, _service_url
    import httpx

    url = _service_url()
    if not url:
        return []
    ids: list[str] = []
    page = 1
    async with httpx.AsyncClient(timeout=60) as client:
        while True:
            response = await client.get(
                f"{url}/v1/documents",
                headers=_service_headers(username),
                params={"page": page, "page_size": 100},
            )
            response.raise_for_status()
            payload = response.json()
            items = payload.get("items") if isinstance(payload, dict) else payload
            items = items or []
            for item in items:
                document_id = str(item.get("id") or "")
                if document_id:
                    ids.append(document_id)
            total = payload.get("total") if isinstance(payload, dict) else None
            if len(items) < 100 or (total is not None and len(ids) >= int(total)):
                break
            page += 1
            if page > 1000:  # defensive: never loop forever on a misbehaving service
                break
    return ids


async def _pending_document_ids(username: str) -> set[str]:
    """Document ids this account is still processing (queued/processing).

    Needed to attribute "处理中" to the **path** a document was filed under: the
    knowledge service knows which documents are running, and this app knows which
    path each document belongs to, so the two are intersected per node. Best-effort
    — a failure here must not blank the tree.
    """
    from .knowledge import _service_headers, _service_url
    import httpx

    url = _service_url()
    if not url:
        return set()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{url}/v1/jobs/pending-documents",
                headers=_service_headers(username),
            )
            response.raise_for_status()
            payload = response.json() or {}
        return {str(item) for item in (payload.get("document_ids") or [])}
    except Exception:  # noqa: BLE001 - the counts are decoration
        logger.warning("paths: could not read pending documents", exc_info=True)
        return set()


@router.get("/tree")
async def path_tree_with_contents(request: Request) -> dict[str, Any]:
    """The path tree enriched with what each node holds and which agents see it.

    Separate from ``GET /paths`` because the extra facts are expensive: the
    knowledge document list comes from the knowledge service over HTTP, and the data
    table list from the registry. The plain tree endpoint stays cheap for callers
    that only need the structure (drag-and-drop, pickers).

    Each node carries:
      * ``document_count`` / ``table_count`` — what the node itself holds;
      * ``pending_count`` — how many of its documents are still processing, so the
        UI can point at *which path* has work running instead of showing an
        account-wide total that belongs to no particular case;
      * ``agents`` — the agents granted this node, so the UI can answer "who can
        see this case" without opening anything;
      * ``is_public`` — whether this is the account's public area (root), whose
        contents include everything not yet filed under a path. Surfaced
        explicitly because "unbound means public" is the one rule that is easy to
        misread as "already isolated".
    """
    username = _username(request)
    _ensure_account_root(username)
    try:
        roots = path_store.list_path_tree(username)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"路径列表读取失败：{exc}") from exc

    # Best-effort enrichment: a failure here must not blank the tree, because the
    # structure is still useful (and the caller can retry).
    document_ids: list[str] = []
    try:
        document_ids = await _account_document_ids(username)
    except Exception:  # noqa: BLE001
        logger.warning("paths: knowledge enrichment unavailable", exc_info=True)

    table_ids: list[str] = []
    tables_by_id: dict[str, dict[str, Any]] = {}
    pending_tables: set[str] = set()
    try:
        from ...data_tables.store import (
            SYSTEM_TABLE_IMPORT_JOB, SYSTEM_TABLE_REGISTRY, _get_conn, _put_conn,
        )
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
                # Which tables have an import still running, so the badge can sit
                # on the table's own path rather than on the whole page.
                cur.execute(
                    f"SELECT DISTINCT table_id FROM {SYSTEM_TABLE_IMPORT_JOB} "
                    "WHERE status IN ('pending', 'parsing', 'loading')",
                )
                pending_tables = {str(r["table_id"]) for r in cur.fetchall()}
        finally:
            _put_conn(conn)
    except Exception:  # noqa: BLE001
        logger.warning("paths: data table enrichment unavailable", exc_info=True)

    # Which documents are still being processed, so "处理中" can be attributed to
    # the path that actually holds them. Best-effort like the counts above.
    pending_docs = await _pending_document_ids(username)

    def decorate(node) -> dict[str, Any]:
        data = node.as_dict()
        # Knowledge is counted per document because that is the unit of access;
        # counting spaces would show a whole account's material on every node that
        # happens to hold the account's single space.
        visible_docs = path_store.effective_document_ids_for_path(
            node.id, username, document_ids,
        ) or []
        visible_tables = path_store.effective_table_ids_for_path(
            node.id, username, table_ids,
        ) or []
        data["document_count"] = len(visible_docs)
        data["table_count"] = len(visible_tables)
        # Intersect with *this node's* visible documents: a pending document filed
        # under another path must not make this node look busy. Only the path that
        # holds it shows the badge, which is the point of moving it off the
        # status column.
        data["pending_count"] = sum(1 for doc_id in visible_docs if doc_id in pending_docs)
        # Same attribution for imports: only the path holding a busy table shows it.
        data["pending_table_count"] = sum(
            1 for table_id in visible_tables if table_id in pending_tables
        )
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


class AgentPathsRequest(BaseModel):
    agent_id: str = Field(min_length=1)
    path_ids: list[str] = Field(default_factory=list)


def _require_owned_agent(agent_id: str, username: str) -> str:
    """Reject an agent that is not this account's (or not a real one).

    Grants decide what an agent can read, so writing them for an id the caller does
    not own would let one account hand access to another's agent. The console
    exposes this through the chat header, where the agent comes from the client.
    """
    if agent_id not in _owned_agent_ids(username):
        raise HTTPException(status_code=403, detail="该智能体不属于当前账号。")
    return agent_id


@router.get("/agent-grants")
async def get_agent_grants(request: Request, agent_id: str) -> dict[str, Any]:
    """Which paths one agent may read — the reverse of ``/grants``.

    The chat header needs this for the currently selected agent alone, and the
    per-agent shape lets the UI render exactly what that agent can reach without
    fetching every agent's grants.
    """
    username = _username(request)
    _require_owned_agent(agent_id, username)
    try:
        granted = path_store.agent_path_ids(agent_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"授权读取失败：{exc}") from exc
    return {"agent_id": agent_id, "path_ids": sorted(granted)}


@router.put("/agent-grants")
async def set_agent_grants(
    request: Request, body: AgentPathsRequest,
) -> dict[str, Any]:
    """Replace one agent's granted paths.

    Replaces the whole set (rather than adding/removing) so the header's checkboxes
    and the agent's actual reach cannot drift apart. Takes effect on the agent's
    **next** retrieval: the scope is resolved from the database per tool call, so
    every conversation of that agent picks it up without a reload.
    """
    username = _username(request)
    _require_owned_agent(body.agent_id, username)
    for path_id in body.path_ids:
        _path_or_404(path_id, username)
    try:
        granted = path_store.replace_agent_paths(body.agent_id, body.path_ids)
    except path_store.PathScopeError as exc:
        raise _handle_scope_error(exc) from exc
    return {"agent_id": body.agent_id, "path_ids": sorted(granted)}


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
    # The path's data-table schema would otherwise linger forever. Failure here is
    # logged, not raised: the path itself is gone, and reporting an error would
    # invite the user to retry a delete that already succeeded.
    try:
        from ...data_tables.store import drop_path_schema

        drop_path_schema(username, path_id)
    except Exception:  # noqa: BLE001
        logger.warning("paths: could not drop the path's data schema", exc_info=True)
    return {"deleted": True, "id": path_id}


@router.get("/assignable")
async def list_assignable_resources(
    request: Request,
    kind: str,
    path_id: str | None = None,
) -> dict[str, Any]:
    """Every resource of one kind, with the path it currently belongs to.

    The picker needs this: assigning material is only meaningful if the user can
    see what is already filed where, otherwise the same table silently gets moved
    between cases with no way to notice. ``path_id`` marks the entries currently
    in that path so the dialog can show them as already included.

    ``kind`` is validated against the store's closed set so a typo cannot produce
    an empty list that reads as "you have no resources".
    """
    username = _username(request)
    if kind not in path_store.RESOURCE_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"未知的资源类型 {kind!r}，可选：{', '.join(path_store.RESOURCE_KINDS)}",
        )
    if path_id is not None:
        _path_or_404(path_id, username)

    items: list[dict[str, Any]] = []
    try:
        if kind == path_store.KIND_DATA_TABLE:
            from ...data_tables.store import SYSTEM_TABLE_REGISTRY, _get_conn, _put_conn
            import psycopg2.extras

            conn = _get_conn()
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        f"SELECT id, display_name, table_name, row_count "
                        f"FROM {SYSTEM_TABLE_REGISTRY} "
                        "WHERE owner_username = %s ORDER BY created_at DESC",
                        (username,),
                    )
                    rows = cur.fetchall()
            finally:
                _put_conn(conn)
            for row in rows:
                resource_id = str(row["id"])
                items.append(
                    {
                        "id": resource_id,
                        "name": row["display_name"] or row["table_name"],
                        "detail": f"{row['row_count'] or 0} 行",
                        "current_path_id": path_store.path_of_resource(kind, resource_id),
                    },
                )
        else:
            # Documents, so the picker can move individual files between cases.
            # Listing spaces here was the reason "管理此路径下的内容" could only
            # ever move an account's entire knowledge at once.
            from .knowledge import _service_headers, _service_url
            import httpx

            url = _service_url()
            if url:
                async with httpx.AsyncClient(timeout=60) as client:
                    page = 1
                    while True:
                        response = await client.get(
                            f"{url}/v1/documents",
                            headers=_service_headers(username),
                            params={"page": page, "page_size": 100},
                        )
                        response.raise_for_status()
                        payload = response.json()
                        documents = (
                            payload.get("items") if isinstance(payload, dict) else payload
                        ) or []
                        for document in documents:
                            resource_id = str(document.get("id") or "")
                            if not resource_id:
                                continue
                            size = document.get("size") or 0
                            items.append(
                                {
                                    "id": resource_id,
                                    "name": str(document.get("filename") or resource_id),
                                    "detail": _human_size(size),
                                    "current_path_id": path_store.path_of_resource(
                                        kind, resource_id,
                                    ),
                                },
                            )
                        total = (
                            payload.get("total") if isinstance(payload, dict) else None
                        )
                        if len(documents) < 100 or (
                            total is not None and len(items) >= int(total)
                        ):
                            break
                        page += 1
                        if page > 1000:  # defensive bound
                            break
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - report rather than showing nothing
        logger.warning("paths: could not list assignable resources", exc_info=True)
        raise HTTPException(status_code=503, detail=f"资源列表读取失败：{exc}") from exc

    for item in items:
        item["in_this_path"] = bool(path_id) and item["current_path_id"] == path_id
        # An unassigned resource is public (root) — the same rule the tree and the
        # query tools apply, so the picker's wording matches the model's reality.
        item["is_public"] = item["current_path_id"] is None
    return {"kind": kind, "path_id": path_id, "items": items}


# --- resource bindings -----------------------------------------------------


@router.get("/{path_id}/resources")
async def list_path_resources(request: Request, path_id: str) -> dict[str, Any]:
    username = _username(request)
    _path_or_404(path_id, username)
    items = path_store.resources_in_path(path_id)
    return {"path_id": path_id, "resources": items}


def _move_table_storage(resource_kind: str, resource_id: str, path_id: str | None) -> None:
    """Make a data table's physical storage match its membership.

    The binding row alone does not isolate anything: the agent-side ``search_path``
    is derived from the granted paths, so a table bound to a case while still
    physically in ``public`` is unreachable for that case's own agent and still
    reachable for every root-granted agent. Moving the table into the path's
    schema is the step that turns membership into isolation.

    Knowledge documents need nothing here — their access is decided by the
    document id set, which the binding lookup already governs.
    """
    if resource_kind != path_store.KIND_DATA_TABLE:
        return
    from ...data_tables.store import TableNotFound, move_table_for_path

    try:
        move_table_for_path(resource_id, path_id)
    except TableNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/{path_id}/resources")
async def bind_resource(
    request: Request, path_id: str, body: BindResourceRequest,
) -> dict[str, Any]:
    """Attach a knowledge document or data table to a path.

    Binding *moves* the resource if it was in another path — the one-resource-one-
    path invariant is a primary key in the store, so this is the only way to
    reinterpret it. For a data table the physical storage is moved too, so the
    binding actually takes effect (see :func:`_move_table_storage`).

    Order matters: the move happens **before** the binding is written. A rejected
    move (bad id, unknown table) then leaves nothing half-done. The reverse order
    would leave a table bound to a case while still physically public — visible to
    the wrong agents, which is the failure this feature exists to prevent.
    """
    username = _username(request)
    _path_or_404(path_id, username)
    _move_table_storage(body.resource_kind, body.resource_id, path_id)
    try:
        path_store.bind_resource(path_id, body.resource_kind, body.resource_id)
    except path_store.PathScopeError as exc:
        # Restore the previous physical location so the two do not disagree.
        try:
            _move_table_storage(
                body.resource_kind, body.resource_id,
                path_store.path_of_resource(body.resource_kind, body.resource_id),
            )
        except Exception:  # noqa: BLE001 - the original error is the useful one
            logger.warning("paths: could not restore table storage", exc_info=True)
        raise _handle_scope_error(exc) from exc
    except Exception:
        # The table now sits in the target schema but is unbound. That is
        # fail-closed (nobody can reach it) and a retry heals it, because the move
        # reads the table's *actual* schema rather than the binding's.
        logger.warning("paths: table moved but binding failed", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="表数据已移动，但归属未写入。请重试以完成。",
        )
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
    try:
        # ``None`` puts the table back in ``public``, matching "unbound = public".
        _move_table_storage(resource_kind, resource_id, None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("paths: could not move table storage", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"已解除归属，但表数据未能移回公共区（{exc}）。请重试。",
        ) from exc
    return {"unbound": True}


@router.delete("/resources/{resource_kind}/{resource_id}")
async def unbind_resource_any_path(
    request: Request, resource_kind: str, resource_id: str,
) -> dict[str, Any]:
    """Return a resource to the public area without naming its current path.

    The listing pages work with documents, not with which path each one sits in,
    so "移回公共区" has no path id to send. Resolving the binding here also keeps
    the ownership check meaningful: the client cannot pass a stale id belonging to
    another account's path.

    Safe to declare after ``/{path_id}/resources/...`` because the two differ in
    segment count (3 vs 4), so ``resources`` can never be captured as a path id.
    """
    username = _username(request)
    current = path_store.path_of_resource(resource_kind, resource_id)
    if current is not None:
        _path_or_404(current, username)
    try:
        path_store.unbind_resource(resource_kind, resource_id)
    except path_store.PathScopeError as exc:
        raise _handle_scope_error(exc) from exc
    try:
        _move_table_storage(resource_kind, resource_id, None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("paths: could not move table storage", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"已解除归属，但表数据未能移回公共区（{exc}）。请重试。",
        ) from exc
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
