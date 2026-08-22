from __future__ import annotations

import asyncio
import hmac
import json
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .config import Settings
from .lightrag_bridge import LightRAGBridge
from .providers import ModelClient
from .store import Store

settings = Settings.from_env()
settings.ensure_dirs()
store = Store(settings.database_url)
lightrag = LightRAGBridge(settings)


def _safe_filename(name: str) -> str:
    clean = Path(name or "unnamed").name
    clean = re.sub(r"[^\w.\-()\u4e00-\u9fff ]", "_", clean).strip(" .")
    return clean[:180] or "unnamed"


def require_api_key(
    x_knowledge_key: str | None = Header(default=None),
) -> None:
    if settings.api_key and not hmac.compare_digest(x_knowledge_key or "", settings.api_key):
        raise HTTPException(status_code=401, detail="Invalid knowledge service key")


def request_scope(
    x_user_id: str | None = Header(default=None),
    x_is_admin: str | None = Header(default=None),
) -> tuple[str, bool]:
    return x_user_id or "default", (x_is_admin or "").lower() in {"1", "true", "yes"}


class SpaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class QueryRequest(BaseModel):
    space_id: str
    query: str = Field(min_length=2, max_length=100000)
    top_k: int = Field(default=20, ge=1, le=100)
    include_relations: bool = True
    include_evidence: bool = True


class SearchRequest(BaseModel):
    space_id: str
    query: str = Field(min_length=2, max_length=100000)
    top_k: int = Field(default=20, ge=1, le=100)
    include_relations: bool = True
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)


class DocumentDetailRequest(BaseModel):
    document_id: str
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    max_chunks: int = Field(default=50, ge=1, le=200)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.ensure_dirs()
    yield


app = FastAPI(title="汇智知识服务", version="0.1.4", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    from .parsers import MinerUParser

    mineru = MinerUParser(settings.mineru_command)
    return {
        "status": "ok",
        "service": "huizhi-knowledge",
        "parser_backend": settings.parser_backend,
        "mineru_configured": bool(settings.mineru_command),
        "mineru_ready": mineru.available,
        "lightrag": await lightrag.health(),
        "embedding_configured": bool(settings.embedding_base_url and settings.embedding_model),
        "reranker_configured": settings.reranker_enabled and bool(settings.reranker_base_url and settings.reranker_model),
    }


@app.post("/v1/spaces", dependencies=[Depends(require_api_key)])
async def create_space(body: SpaceRequest, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    owner_id, _ = scope
    return store.create_space(body.name, owner_id)


@app.get("/v1/spaces", dependencies=[Depends(require_api_key)])
async def list_spaces(scope: tuple[str, bool] = Depends(request_scope)) -> list[dict[str, Any]]:
    owner_id, is_admin = scope
    return store.list_spaces(owner_id, is_admin)


@app.post("/v1/documents/upload", dependencies=[Depends(require_api_key)])
async def upload_document(
    file: UploadFile = File(...),
    space_id: str = Form(...),
    scope: tuple[str, bool] = Depends(request_scope),
) -> dict[str, Any]:
    owner_id, is_admin = scope
    if not store.space_exists(space_id, owner_id, is_admin):
        raise HTTPException(status_code=404, detail="Knowledge space not found")
    filename = _safe_filename(file.filename or "unnamed")
    suffix = Path(filename).suffix.lower()
    allowed = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".csv", ".txt", ".md"}
    if suffix not in allowed:
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {suffix or 'unknown'}")
    document_id = str(uuid.uuid4())
    target_dir = settings.files_dir / space_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{document_id}-{filename}"
    size = 0
    try:
        with target_path.open("wb") as output:
            while block := await file.read(1024 * 1024):
                size += len(block)
                if size > settings.max_file_mb * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="File exceeds configured size limit")
                output.write(block)
    except Exception:
        target_path.unlink(missing_ok=True)
        raise
    created = store.create_document(space_id, owner_id, filename, str(target_path), file.content_type, document_id=document_id)
    return {**created, "size": size}


@app.get("/v1/documents/{document_id}", dependencies=[Depends(require_api_key)])
async def get_document(document_id: str, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    owner_id, is_admin = scope
    document = store.get_document(document_id, owner_id, is_admin)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    return document


@app.post("/v1/documents/{document_id}/reprocess", dependencies=[Depends(require_api_key)])
async def reprocess_document(document_id: str, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    """Requeue an existing document so improved parsers can rebuild its index."""
    owner_id, is_admin = scope
    queued = store.requeue_document(document_id, owner_id, is_admin)
    if not queued:
        raise HTTPException(status_code=404, detail="Document or processing job not found")
    return queued


@app.get("/v1/documents/{document_id}/file", dependencies=[Depends(require_api_key)])
async def get_document_file(document_id: str, scope: tuple[str, bool] = Depends(request_scope)) -> FileResponse:
    """返回原始文件，支持PDF/DOCX/TXT等格式的带格式预览"""
    owner_id, is_admin = scope
    document = store.get_document(document_id, owner_id, is_admin)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    file_path = Path(document["path"])
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")
    media_type = document.get("media_type") or "application/octet-stream"
    return FileResponse(path=str(file_path), media_type=media_type, filename=document["filename"])


@app.get("/v1/spaces/{space_id}/documents", dependencies=[Depends(require_api_key)])
async def list_documents(space_id: str, scope: tuple[str, bool] = Depends(request_scope)) -> list[dict[str, Any]]:
    owner_id, is_admin = scope
    if not store.space_exists(space_id, owner_id, is_admin):
        raise HTTPException(status_code=404, detail="Knowledge space not found")
    return store.list_documents(space_id, owner_id, is_admin)


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(require_api_key)])
async def get_job(job_id: str, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    owner_id, is_admin = scope
    job = store.get_job(job_id, owner_id, is_admin)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/v1/query", dependencies=[Depends(require_api_key)])
async def query(body: QueryRequest, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    owner_id, is_admin = scope
    if not store.space_exists(body.space_id, owner_id, is_admin):
        raise HTTPException(status_code=404, detail="Knowledge space not found")
    local_evidence = store.entity_context(body.space_id, body.query, body.top_k)
    direct = store.search(body.space_id, body.query, body.top_k)
    vector = await asyncio.to_thread(ModelClient(settings).embed, [body.query])
    semantic = store.search_by_embedding(body.space_id, vector[0], body.top_k) if vector else []
    merged: dict[str, dict[str, Any]] = {}
    for item in [*local_evidence, *direct, *semantic]:
        key = item.get("id") or f"{item.get('document_id')}:{item.get('page')}:{item.get('location')}"
        merged[key] = item
    evidence = list(merged.values())[: body.top_k]
    relations = store.relation_context(body.space_id, body.query, body.top_k) if body.include_relations else []
    remote = await lightrag.query(body.query)
    remote_answer = remote.get("response") if isinstance(remote, dict) else None
    model_answer = remote_answer or await asyncio.to_thread(ModelClient(settings).answer, body.query, evidence, relations)
    return {
        "answer": model_answer,
        "evidence": evidence if body.include_evidence else [],
        "relations": relations,
        "semantic_search_enabled": bool(vector),
        "lightrag": remote,
    }


@app.get("/v1/evidence/{evidence_id}", dependencies=[Depends(require_api_key)])
async def evidence(evidence_id: str, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    item = store.get_evidence(evidence_id)
    if not item:
        raise HTTPException(status_code=404, detail="Evidence not found")
    owner_id, is_admin = scope
    document = store.get_document(item["document_id"], owner_id, is_admin)
    if not document:
        raise HTTPException(status_code=404, detail="Evidence not found")
    return item


@app.post("/v1/query/stream", dependencies=[Depends(require_api_key)])
async def query_stream(body: QueryRequest, scope: tuple[str, bool] = Depends(request_scope)) -> StreamingResponse:
    """流式查询接口，返回SSE格式的流式响应"""
    owner_id, is_admin = scope
    if not store.space_exists(body.space_id, owner_id, is_admin):
        raise HTTPException(status_code=404, detail="Knowledge space not found")

    # 检索证据
    local_evidence = store.entity_context(body.space_id, body.query, body.top_k)
    direct = store.search(body.space_id, body.query, body.top_k)
    vector = await asyncio.to_thread(ModelClient(settings).embed, [body.query])
    semantic = store.search_by_embedding(body.space_id, vector[0], body.top_k) if vector else []
    merged: dict[str, dict[str, Any]] = {}
    for item in [*local_evidence, *direct, *semantic]:
        key = item.get("id") or f"{item.get('document_id')}:{item.get('page')}:{item.get('location')}"
        merged[key] = item
    evidence = list(merged.values())[: body.top_k]
    relations = store.relation_context(body.space_id, body.query, body.top_k) if body.include_relations else []

    def generate():
        # 先发送证据信息
        evidence_data = evidence if body.include_evidence else []
        yield f"data: {json.dumps({'type': 'evidence', 'data': evidence_data})}\n\n"

        # 流式生成回答
        model_client = ModelClient(settings)
        for chunk in model_client.answer_stream(body.query, evidence, relations):
            yield f"data: {json.dumps({'type': 'answer', 'data': chunk})}\n\n"

        # 发送完成信号
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/v1/search", dependencies=[Depends(require_api_key)])
async def search(body: SearchRequest, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    """纯检索接口，只返回检索结果，不生成answer"""
    owner_id, is_admin = scope
    if not store.space_exists(body.space_id, owner_id, is_admin):
        raise HTTPException(status_code=404, detail="Knowledge space not found")

    # 执行多种检索
    local_evidence = store.entity_context(body.space_id, body.query, body.top_k)
    direct = store.search(body.space_id, body.query, body.top_k)
    vector = await asyncio.to_thread(ModelClient(settings).embed, [body.query])
    semantic = store.search_by_embedding(body.space_id, vector[0], body.top_k) if vector else []

    # 合并去重
    merged: dict[str, dict[str, Any]] = {}
    for item in [*local_evidence, *direct, *semantic]:
        key = item.get("id") or f"{item.get('document_id')}:{item.get('page')}:{item.get('location')}"
        if key not in merged:
            merged[key] = item

    # 按分数过滤和排序
    evidence = list(merged.values())
    if body.min_score > 0:
        evidence = [item for item in evidence if (item.get("score") or 0) >= body.min_score]
    evidence = evidence[:body.top_k]

    # 获取关系
    relations = store.relation_context(body.space_id, body.query, body.top_k) if body.include_relations else []

    # 按文档分组统计
    doc_groups: dict[str, dict[str, Any]] = {}
    for item in evidence:
        doc_id = item.get("document_id")
        if doc_id not in doc_groups:
            doc_groups[doc_id] = {
                "document_id": doc_id,
                "filename": item.get("filename"),
                "snippet_count": 0,
                "pages": [],
            }
        doc_groups[doc_id]["snippet_count"] += 1
        page = item.get("page")
        if page and page not in doc_groups[doc_id]["pages"]:
            doc_groups[doc_id]["pages"].append(page)

    return {
        "query": body.query,
        "evidence": evidence,
        "documents": list(doc_groups.values()),
        "relations": relations,
        "total_count": len(evidence),
        "document_count": len(doc_groups),
        "semantic_search_enabled": bool(vector),
    }


@app.post("/v1/documents/chunks", dependencies=[Depends(require_api_key)])
async def get_document_chunks(body: DocumentDetailRequest, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    """获取指定文档的详细片段"""
    owner_id, is_admin = scope
    document = store.get_document(body.document_id, owner_id, is_admin)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    # 获取文档的所有evidence
    chunks = store.get_document_evidence(body.document_id)

    # 按页码过滤
    if body.page_start is not None:
        chunks = [c for c in chunks if (c.get("page") or 0) >= body.page_start]
    if body.page_end is not None:
        chunks = [c for c in chunks if (c.get("page") or 0) <= body.page_end]

    total_count = len(chunks)

    # 限制数量
    chunks = chunks[:body.max_chunks]

    return {
        "document_id": body.document_id,
        "filename": document.get("filename"),
        "chunks": chunks,
        "total_count": total_count,
    }


@app.get("/v1/documents/{document_id}/evidence", dependencies=[Depends(require_api_key)])
async def list_document_evidence(
    document_id: str,
    page: int | None = None,
    limit: int = 50,
    scope: tuple[str, bool] = Depends(request_scope),
) -> dict[str, Any]:
    """列出文档的所有evidence片段"""
    owner_id, is_admin = scope
    document = store.get_document(document_id, owner_id, is_admin)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    evidence = store.get_document_evidence(document_id)
    if page is not None:
        evidence = [e for e in evidence if e.get("page") == page]

    return {
        "document_id": document_id,
        "filename": document.get("filename"),
        "evidence": evidence[:limit],
        "total_count": len(evidence),
    }
