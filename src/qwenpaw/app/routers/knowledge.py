"""Authenticated proxy to the standalone Huizhi knowledge service."""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .auth import _request_username

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
        async with httpx.AsyncClient(timeout=5) as client:
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


@router.post("/documents/upload")
async def upload_knowledge_document(
    request: Request,
    space_id: str = Form(default=""),
    file: UploadFile = File(...),
) -> Any:
    username = _request_username(request)
    content = await file.read()
    return await _forward(
        request,
        "POST",
        "/v1/documents/upload",
        username,
        files={"file": (file.filename or "unnamed", content, file.content_type or "application/octet-stream")},
        data={"space_id": space_id},
    )


@router.get("/documents/{document_id}")
async def get_knowledge_document(document_id: str, request: Request) -> Any:
    return await _forward(request, "GET", f"/v1/documents/{document_id}", _request_username(request))


@router.post("/documents/{document_id}/reprocess")
async def reprocess_knowledge_document(document_id: str, request: Request) -> Any:
    return await _forward(request, "POST", f"/v1/documents/{document_id}/reprocess", _request_username(request))


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
) -> Any:
    params: dict[str, str] = {}
    if filename:
        params["filename"] = filename
    if created_after:
        params["created_after"] = created_after
    if created_before:
        params["created_before"] = created_before
    if doc_ids:
        params["doc_ids"] = doc_ids
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


@router.get("/documents/{document_id}/preview")
async def preview_knowledge_document(document_id: str, request: Request) -> Any:
    return await _forward(request, "GET", f"/v1/documents/{document_id}/preview", _request_username(request))


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
