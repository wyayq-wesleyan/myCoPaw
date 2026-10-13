"""Authenticated proxy to the standalone Huizhi knowledge service."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .auth import _request_username

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class KnowledgeQueryRequest(BaseModel):
    space_id: str = Field(default="", max_length=200)
    query: str = Field(min_length=2, max_length=100000)
    top_k: int = Field(default=20, ge=1, le=100)
    include_relations: bool = True
    include_evidence: bool = True


class KnowledgeSearchRequest(BaseModel):
    space_id: str = Field(default="", max_length=200)
    query: str = Field(min_length=2, max_length=100000)
    top_k: int = Field(default=20, ge=1, le=100)
    include_relations: bool = True
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)


class DocumentChunksRequest(BaseModel):
    document_id: str = Field(min_length=1, max_length=200)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    max_chunks: int = Field(default=20, ge=1, le=200)



def _service_url() -> str:
    return os.environ.get("QWENPAW_KNOWLEDGE_URL", "").strip().rstrip("/")


def workspace_for_path(path_id: str) -> str:
    """Graph workspace name for a path.

    Mirrors ``knowledge-service/app/graph_store.py::workspace_for_path`` (and the
    data-space schema naming) so one path is recognisable in every store. Kept as a
    small duplicate rather than a cross-service import: the two services deploy
    separately and must not share a code dependency.
    """
    compact = str(path_id or "").lower().replace("-", "").strip()
    return f"ws_{compact}" if compact else ""


def _workspace_for_root(username: str) -> str:
    """The public area's workspace (the account root path)."""
    try:
        from ...platform_store import paths as path_store

        return workspace_for_path(path_store.ensure_root_path(username))
    except Exception:  # noqa: BLE001 - unresolvable root means no workspace
        logger.warning("knowledge: could not resolve the root path", exc_info=True)
        return ""


def _service_headers(username: str) -> dict[str, str]:
    headers = {"X-User-Id": username, "X-Is-Admin": "true" if username == "admin" else "false"}
    key = os.environ.get("QWENPAW_KNOWLEDGE_API_KEY", "").strip()
    if key:
        headers["X-Knowledge-Key"] = key
    return headers


@router.get("/health")
async def knowledge_health(request: Request) -> dict[str, Any]:
    username = _request_username(request)
    url = _service_url()
    if not url:
        return {"configured": False, "status": "disabled", "user": username}
    try:
        # The knowledge service may need a few seconds to perform its cached
        # model capability probes after startup.  Keep the proxy timeout above
        # the probe timeout instead of incorrectly reporting it as offline.
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{url}/health")
            response.raise_for_status()
            return {"configured": True, "status": "ok", "service": response.json()}
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"Knowledge service unavailable: {exc}") from exc


@router.post("/query")
async def query_knowledge(body: KnowledgeQueryRequest, request: Request) -> dict[str, Any]:
    username = _request_username(request)
    url = _service_url()
    if not url:
        raise HTTPException(status_code=503, detail="Knowledge service is not configured")
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(
                f"{url}/v1/query",
                headers=_service_headers(username),
                json=body.model_dump(),
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:2000]
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"Knowledge service unavailable: {exc}") from exc


async def _forward(
    request: Request,
    method: str,
    path: str,
    username: str,
    **kwargs: Any,
) -> Any:
    url = _service_url()
    if not url:
        raise HTTPException(status_code=503, detail="Knowledge service is not configured")
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.request(method, f"{url}{path}", headers=_service_headers(username), **kwargs)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text[:2000]) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"Knowledge service unavailable: {exc}") from exc


async def _account_pending_count(request: Request) -> int:
    """How many of this account's documents are still queued or processing.

    Deliberately *not* derived from the current page or path: it is the answer to
    "is background work still running?", which the UI shows persistently. A short
    document list must not make it read as zero — that is exactly how the
    indicator used to vanish when the user switched to an empty path.

    Best-effort: an unreachable service yields 0 rather than failing the whole
    listing, because the list itself is still useful without the count.
    """
    url = _service_url()
    if not url:
        return 0
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{url}/v1/jobs/pending-count",
                headers=_service_headers(_request_username(request)),
            )
            response.raise_for_status()
            return int((response.json() or {}).get("pending") or 0)
    except Exception:  # noqa: BLE001 - the count must never break the listing
        logger.warning("knowledge: could not read the pending count", exc_info=True)
        return 0


async def migrate_legacy_graph_workspaces() -> dict[str, Any]:
    """Rebuild every account's knowledge graph into per-path workspaces.

    Runs at startup, after the path model exists (so each account's root path is
    known). Kept in the main app because **it owns the path model** — the knowledge
    service cannot name a workspace on its own.

    Only the **root** (public) workspace is populated: every document that existed
    before this feature was unfiled, which by definition is the public area. Filing
    a document into a case afterwards moves it (see the transfer flow).

    Idempotent: the knowledge service skips documents already present, so a rerun
    after a failure only does the remaining work. Best-effort by design — a graph
    outage must not stop the app from starting.
    """
    url = _service_url()
    if not url:
        return {"enabled": False, "reason": "knowledge service not configured"}

    from ...config.utils import load_config
    from ...platform_store import paths as path_store
    from ..user_space import user_agent_ids

    try:
        owners = sorted({
            getattr(profile, "owner_username", None)
            for profile in load_config().agents.profiles.values()
            if getattr(profile, "owner_username", None)
        })
    except Exception:  # noqa: BLE001
        logger.warning("graph-migration: could not list accounts", exc_info=True)
        return {"enabled": False, "reason": "accounts unavailable"}

    total = {"indexed": 0, "skipped": 0, "failed": 0, "accounts": 0}
    for owner in owners:
        try:
            # Documents reachable through the account's root path today. Anything
            # unfiled belongs to the root, and the root workspace is where the
            # pre-existing material has to land.
            root_path = path_store.ensure_root_path(owner)
            workspace = workspace_for_path(root_path)
            async with httpx.AsyncClient(timeout=60) as client:
                page = 1
                mappings: list[dict[str, str]] = []
                while True:
                    response = await client.get(
                        f"{url}/v1/documents",
                        headers=_service_headers(owner),
                        params={"page": page, "page_size": 100},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    items = payload.get("items") if isinstance(payload, dict) else payload
                    items = items or []
                    mappings.extend(
                        {"document_id": str(item.get("id")), "workspace": workspace}
                        for item in items
                        if item.get("id")
                    )
                    total_docs = payload.get("total") if isinstance(payload, dict) else None
                    if len(items) < 100 or (
                        total_docs is not None and len(mappings) >= int(total_docs)
                    ):
                        break
                    page += 1
                    if page > 1000:
                        break
                if not mappings:
                    continue
                # The rebuild can take minutes (entity extraction); allow for it.
                migrate = await client.post(
                    f"{url}/v1/graph/migrate",
                    headers=_service_headers(owner),
                    json={"mappings": mappings},
                    timeout=httpx.Timeout(3600.0, connect=10.0),
                )
                migrate.raise_for_status()
                report = migrate.json() or {}
                total["indexed"] += int(report.get("indexed") or 0)
                total["skipped"] += int(report.get("skipped") or 0)
                total["failed"] += int(report.get("failed") or 0)
                total["accounts"] += 1
        except Exception:  # noqa: BLE001 - one account must not stop the rest
            logger.warning("graph-migration: failed for %s", owner, exc_info=True)
            total["failed"] += 1
    logger.info("graph-migration: %s", total)
    return {"enabled": True, **total}


async def _resolve_path_document_filter(
    request: Request,
    path_id: str,
) -> list[str] | None:
    """Document ids visible under one path, or ``None`` if the path is not ours.

    Access is resolved at **document** granularity: a path holds individual files,
    so a case folder shows exactly the files put in it, and an unfiled file shows
    under the root (public) path.

    Returns an explicit id list rather than a predicate because the documents live
    in the knowledge service: resolving here and filtering there keeps the path
    model in one place (the platform store) instead of teaching the knowledge
    service about it.

    ``None`` means "unknown or foreign path" — the caller must treat that as *no
    access*, never as "no filter".
    """
    username = _request_username(request)
    from ...platform_store import paths as path_store

    url = _service_url()
    if not url:
        raise HTTPException(status_code=503, detail="Knowledge service is not configured")
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            page = 1
            documents: list[str] = []
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
                documents.extend(
                    str(item.get("id") or "") for item in items if item.get("id")
                )
                total = payload.get("total") if isinstance(payload, dict) else None
                if len(items) < 100 or (
                    total is not None and len(documents) >= int(total)
                ):
                    break
                page += 1
                if page > 1000:  # defensive bound
                    break
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"Knowledge service unavailable: {exc}") from exc

    return path_store.effective_document_ids_for_path(path_id, username, documents)


@router.post("/documents/upload")
async def upload_knowledge_document(
    request: Request,
    space_id: str = Form(default=""),
    path_id: str = Form(default=""),
    file: UploadFile = File(...),
) -> Any:
    """Upload a document, filing it under *path_id* when one is given.

    ``path_id`` exists because the console uploads **while a path is selected**:
    the file must land in the path the user is looking at. Before this, the target
    path was never sent, so every upload silently became public — the user picked
    测试1, the document went to 公共区, and selecting the path again did not bring
    it back.

    An empty ``path_id`` means the public area, which matches the rest of the model
    ("unbound = public"). A path that is not this account's is refused *before* the
    upload so a typo cannot scatter files into the wrong case.
    """
    username = _request_username(request)

    # Validate the destination *before* the service check: a bad path is the
    # caller's mistake and should read as "路径不存在" rather than being masked by
    # an unrelated 503 when the knowledge service happens to be unconfigured.
    from ...platform_store import paths as path_store

    if path_id:
        node = path_store.path_by_id(path_id)
        if node is None or node["owner_username"] != username:
            raise HTTPException(status_code=404, detail="路径不存在。")

    url = _service_url()
    if not url:
        raise HTTPException(status_code=503, detail="Knowledge service is not configured")

    # The graph workspace for this upload's path. An empty path_id means the public
    # area, i.e. the account root.
    target_workspace = (
        workspace_for_path(path_id) if path_id else _workspace_for_root(username)
    )

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3600.0, connect=10.0)) as client:
            health = await client.get(f"{url}/health")
            health.raise_for_status()
            readiness = health.json()
            if not readiness.get("upload_ready"):
                raise HTTPException(
                    status_code=409,
                    detail=readiness.get("upload_block_reason")
                    or "Knowledge upload requires an embedding model",
                )
            response = await client.post(
                f"{url}/v1/documents/upload",
                headers=_service_headers(username),
                files={"file": (file.filename or "unnamed", file.file, file.content_type or "application/octet-stream")},
                data={"space_id": space_id, "workspace": target_workspace},
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text[:2000]) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"Knowledge service unavailable: {exc}") from exc

    # File it after a successful upload: binding first would leave a membership row
    # pointing at a document that was never created.
    document_id = str((payload or {}).get("id") or "")
    if path_id and document_id:
        try:
            path_store.bind_resource(
                path_id, path_store.KIND_KNOWLEDGE_DOCUMENT, document_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("knowledge: could not file upload %s", document_id, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"文件已上传，但归入路径失败（{exc}）。请在列表中重新归入。",
            ) from exc
    if isinstance(payload, dict):
        payload["path_id"] = path_id or None
    return payload


@router.get("/documents/{document_id}")
async def get_knowledge_document(document_id: str, request: Request) -> Any:
    return await _forward(request, "GET", f"/v1/documents/{document_id}", _request_username(request))


@router.post("/documents/{document_id}/reprocess")
async def reprocess_knowledge_document(document_id: str, request: Request) -> Any:
    return await _forward(request, "POST", f"/v1/documents/{document_id}/reprocess", _request_username(request))


@router.delete("/documents/{document_id}")
async def delete_knowledge_document(document_id: str, request: Request) -> Any:
    """Delete a document and drop its path membership.

    The membership row lives in the platform store, which the knowledge service
    knows nothing about, so forwarding the delete alone left the binding behind
    forever. Those rows grant nothing once the document is gone, but they are a
    leak — and ``resource_bindings`` is keyed by resource id, so a reused id would
    silently inherit the old document's case.

    The unbind runs **after** a successful delete so a failed delete cannot strip a
    still-existing document of its path.
    """
    result = await _forward(
        request, "DELETE", f"/v1/documents/{document_id}", _request_username(request),
    )
    try:
        from ...platform_store import paths as path_store

        current = path_store.path_of_resource(
            path_store.KIND_KNOWLEDGE_DOCUMENT, document_id,
        )
        if current is not None:
            path_store.unbind_resource(path_store.KIND_KNOWLEDGE_DOCUMENT, document_id)
    except Exception:  # noqa: BLE001 - the document is already deleted
        logger.warning(
            "knowledge: could not clear the path binding for deleted document %s",
            document_id,
            exc_info=True,
        )
    return result


@router.get("/documents/{document_id}/file")
async def get_knowledge_document_file(document_id: str, request: Request):
    """代理原始文件请求，返回文件流"""
    username = _request_username(request)
    url = _service_url()
    if not url:
        raise HTTPException(status_code=503, detail="Knowledge service is not configured")
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.get(
                f"{url}/v1/documents/{document_id}/file",
                headers=_service_headers(username),
            )
            response.raise_for_status()
            content_type = response.headers.get("content-type", "application/octet-stream")
            content_disp = response.headers.get("content-disposition", "")
            from fastapi.responses import Response
            return Response(
                content=response.content,
                media_type=content_type,
                headers={"Content-Disposition": content_disp} if content_disp else {},
            )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text[:2000]) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"Knowledge service unavailable: {exc}") from exc


@router.get("/documents")
async def list_knowledge_documents_v2(
    request: Request,
    filename: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    doc_ids: str | None = None,
    status: str | None = None,
    path_id: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> Any:
    """List the account's documents, optionally narrowed to one path.

    ``path_id`` filtering is applied **here**, in the main app, not in the
    knowledge service: the path model (and the rule that an unfiled document
    belongs to the account's public area) lives in the platform store, which the
    knowledge service does not depend on. The document id set is resolved from the
    path first, then handed to the knowledge service as an explicit id filter.

    ``pending_count`` is **account-wide** and therefore computed independently of
    every filter above. It answers "is background processing still running?", a
    fact about the account rather than about the current view — earlier revisions
    returned ``0`` from the short-circuit branches below, so the indicator
    disappeared as soon as the user switched to a path with nothing pending.
    """
    pending = await _account_pending_count(request)

    params: dict[str, str] = {}
    if filename:
        params["filename"] = filename
    if created_after:
        params["created_after"] = created_after
    if created_before:
        params["created_before"] = created_before
    if doc_ids:
        params["doc_ids"] = doc_ids
    if status:
        params["status"] = status

    def empty_page() -> dict[str, Any]:
        return {
            "items": [], "total": 0, "page": page,
            "page_size": page_size, "pending_count": pending,
        }

    if path_id:
        resolved = await _resolve_path_document_filter(request, path_id)
        if resolved is None:
            # Unknown / foreign path: return an empty page rather than falling
            # back to "everything", which would leak other entries into a case.
            return empty_page()
        if not resolved:
            return empty_page()
        if doc_ids:
            # Both filters can arrive at once (keyword search + a selected path).
            # The knowledge service accepts a single ``doc_ids`` list, so the two
            # narrower sets have to be intersected here — assigning one over the
            # other silently dropped the keyword filter, showing every document in
            # the path as if it had matched.
            allowed = set(resolved)
            kept = [d for d in (part.strip() for part in doc_ids.split(",")) if d in allowed]
            if not kept:
                return empty_page()
            params["doc_ids"] = ",".join(kept)
        else:
            params["doc_ids"] = ",".join(resolved)

    params["page"] = str(page)
    params["page_size"] = str(page_size)
    username = _request_username(request)
    url = _service_url()
    if not url:
        raise HTTPException(status_code=503, detail="Knowledge service is not configured")
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.get(
                f"{url}/v1/documents",
                headers=_service_headers(username),
                params=params,
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text[:2000]) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"Knowledge service unavailable: {exc}") from exc


@router.get("/jobs")
async def list_knowledge_jobs(request: Request) -> Any:
    return await _forward(request, "GET", "/v1/jobs", _request_username(request))


@router.get("/jobs/{job_id}")
async def get_knowledge_job(job_id: str, request: Request) -> Any:
    return await _forward(request, "GET", f"/v1/jobs/{job_id}", _request_username(request))


@router.get("/jobs/{job_id}/log")
async def get_knowledge_job_log(job_id: str, request: Request):
    username = _request_username(request)
    url = _service_url()
    if not url:
        raise HTTPException(status_code=503, detail="Knowledge service is not configured")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(f"{url}/v1/jobs/{job_id}/log", headers=_service_headers(username))
            response.raise_for_status()
            from fastapi.responses import Response
            return Response(
                content=response.content,
                media_type="text/plain",
                headers={"Content-Disposition": response.headers.get("content-disposition", "")},
            )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text[:2000]) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"Knowledge service unavailable: {exc}") from exc


@router.get("/documents/{document_id}/preview")
async def preview_knowledge_document(document_id: str, request: Request) -> Any:
    return await _forward(request, "GET", f"/v1/documents/{document_id}/preview", _request_username(request))


@router.get("/documents/{document_id}/stats")
async def get_document_stats(document_id: str, request: Request) -> Any:
    return await _forward(request, "GET", f"/v1/documents/{document_id}/stats", _request_username(request))


@router.post("/search")
async def search_knowledge(body: KnowledgeSearchRequest, request: Request) -> dict[str, Any]:
    """纯检索接口，只返回检索结果，不生成answer"""
    username = _request_username(request)
    url = _service_url()
    if not url:
        raise HTTPException(status_code=503, detail="Knowledge service is not configured")
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(
                f"{url}/v1/search",
                headers=_service_headers(username),
                json=body.model_dump(),
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:2000]
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"Knowledge service unavailable: {exc}") from exc


class KnowledgeSearchFilterRequest(BaseModel):
    query: str = Field(min_length=2, max_length=100000)
    top_k: int = Field(default=50, ge=1, le=200)


@router.post("/search/filter")
async def search_knowledge_filter(body: KnowledgeSearchFilterRequest, request: Request) -> dict[str, Any]:
    username = _request_username(request)
    url = _service_url()
    if not url:
        raise HTTPException(status_code=503, detail="Knowledge service is not configured")
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(
                f"{url}/v1/search/filter",
                headers=_service_headers(username),
                json=body.model_dump(),
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:2000]
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"Knowledge service unavailable: {exc}") from exc


@router.post("/documents/chunks")
async def get_document_chunks(body: DocumentChunksRequest, request: Request) -> dict[str, Any]:
    """获取指定文档的详细片段"""
    username = _request_username(request)
    url = _service_url()
    if not url:
        raise HTTPException(status_code=503, detail="Knowledge service is not configured")
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(
                f"{url}/v1/documents/chunks",
                headers=_service_headers(username),
                json=body.model_dump(),
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:2000]
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"Knowledge service unavailable: {exc}") from exc


@router.post("/query/stream")
async def query_knowledge_stream(body: KnowledgeQueryRequest, request: Request) -> StreamingResponse:
    """流式查询知识库"""
    username = _request_username(request)
    url = _service_url()
    if not url:
        raise HTTPException(status_code=503, detail="Knowledge service is not configured")

    async def generate():
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream(
                    "POST",
                    f"{url}/v1/query/stream",
                    headers=_service_headers(username),
                    json=body.model_dump(),
                ) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        yield chunk
        except httpx.HTTPStatusError as exc:
            yield f"data: {{\"type\": \"error\", \"data\": \"知识检索失败：{exc.response.status_code}\"}}\n\n"
        except httpx.HTTPError as exc:
            yield f"data: {{\"type\": \"error\", \"data\": \"知识服务暂时不可用：{exc}\"}}\n\n"
        except Exception as exc:
            yield f"data: {{\"type\": \"error\", \"data\": \"知识检索发生错误：{exc}\"}}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
