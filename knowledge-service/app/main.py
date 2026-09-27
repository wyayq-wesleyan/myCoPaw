from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import tempfile
import uuid
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from .config import Settings
from .lightrag_bridge import LightRAGBridge
from .logging_setup import attach_to_uvicorn, configure_logging
from .parsers import extract_text_from_file
from .providers import ModelClient
from .store import Store

settings = Settings.from_env()
settings.ensure_dirs()
# Console + rotating file under KNOWLEDGE_LOG_DIR (bind-mounted to the host).
_log_file_handler = configure_logging("knowledge-api")
logger = logging.getLogger("knowledge-api")
store = Store(settings.database_url)
lightrag = LightRAGBridge(settings)

# 语义检索相关性地板（余弦分数）：实测分布 相关查询 0.45+，无关查询 <=0.36
SEMANTIC_MIN_SCORE = 0.40


def upload_readiness() -> dict[str, Any]:
    """Return the capabilities required to turn files into usable RAG data.

    Image understanding is deliberately not a readiness condition.  The
    worker attempts it only for image chunks and skips it if the configured
    chat model cannot accept images.
    """
    embedding_configured = bool(
        settings.embedding_explicitly_configured
        and settings.embedding_base_url
        and settings.embedding_model
    )
    client = ModelClient(settings)
    embedding_ready = (
        client.supports_embedding() if embedding_configured else False
    )
    worker = store.get_worker_health()
    reasons: list[str] = []
    if not embedding_configured:
        reasons.append("未配置独立的嵌入模型（EMBEDDING_BASE_URL、EMBEDDING_MODEL）")
    elif not embedding_ready:
        reasons.append("嵌入模型不可用，请检查地址、密钥和模型名称")
    if not worker["available"]:
        reasons.append(str(worker["reason"]))
    return {
        # Embeddings are necessary for retrieval. Image descriptions are an
        # optional best-effort enrichment performed by the worker.
        "upload_ready": embedding_ready and worker["available"],
        "embedding_configured": embedding_configured,
        "embedding_available": embedding_ready,
        "worker": worker,
        "upload_block_reason": "；".join(reasons) or None,
        "upload_notice": "文档中的图片仅在当前模型支持多模态时解析；不支持时自动跳过图片，文字和表格仍会正常入库。",
    }


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
    # uvicorn installs its own non-propagating handlers during startup, which
    # happens before this lifespan runs, so attach the file handler now to get
    # the request logs onto the host as well.
    attach_to_uvicorn(_log_file_handler)
    yield


app = FastAPI(title="汇智知识服务", version="0.1.4", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    from .parsers import MinerUParser

    mineru = MinerUParser(settings.mineru_command, settings.mineru_timeout_seconds)
    return {
        "status": "ok",
        "service": "huizhi-knowledge",
        "parser_backend": settings.parser_backend,
        "knowledge_offline": settings.knowledge_offline,
        "mineru_configured": bool(settings.mineru_command),
        "mineru_ready": mineru.available,
        "lightrag": await lightrag.health(),
        **await asyncio.to_thread(upload_readiness),
        "reranker_configured": settings.reranker_enabled and bool(settings.reranker_base_url and settings.reranker_model),
    }


@app.post("/v1/spaces", dependencies=[Depends(require_api_key)])
async def create_space(body: SpaceRequest, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    owner_id, _ = scope
    return await asyncio.to_thread(store.create_space, body.name, owner_id)


@app.get("/v1/spaces", dependencies=[Depends(require_api_key)])
async def list_spaces(scope: tuple[str, bool] = Depends(request_scope)) -> list[dict[str, Any]]:
    owner_id, is_admin = scope
    return await asyncio.to_thread(store.list_spaces, owner_id, is_admin)


@app.post("/v1/documents/upload", dependencies=[Depends(require_api_key)])
async def upload_document(
    file: UploadFile = File(...),
    space_id: str = Form(default=""),
    scope: tuple[str, bool] = Depends(request_scope),
) -> dict[str, Any]:
    readiness = await asyncio.to_thread(upload_readiness)
    if not readiness["upload_ready"]:
        raise HTTPException(
            status_code=409,
            detail=readiness["upload_block_reason"],
        )
    owner_id, is_admin = scope
    if not space_id:
        space_obj = await asyncio.to_thread(store.get_or_create_user_space, owner_id)
        space_id = space_obj["id"]
    elif not await asyncio.to_thread(store.space_exists, space_id, owner_id, is_admin):
        raise HTTPException(status_code=404, detail="Knowledge space not found")
    filename = _safe_filename(file.filename or "unnamed")
    suffix = Path(filename).suffix.lower()
    allowed = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".csv", ".txt", ".md", ".json"}
    if suffix not in allowed:
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {suffix or 'unknown'}")
    document_id = str(uuid.uuid4())
    target_dir = settings.files_dir / space_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{document_id}-{filename}"
    size = 0
    content_digest = hashlib.sha256()
    try:
        with target_path.open("wb") as output:
            while block := await file.read(1024 * 1024):
                size += len(block)
                if size > settings.max_file_mb * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="File exceeds configured size limit")
                output.write(block)
                content_digest.update(block)
    except Exception:
        target_path.unlink(missing_ok=True)
        raise
    content_hash = content_digest.hexdigest()
    existing = await asyncio.to_thread(store.find_document_by_hash, space_id, owner_id, content_hash, size)
    if existing:
        target_path.unlink(missing_ok=True)
        return {
            "id": existing["id"],
            "job_id": existing.get("job_id"),
            "status": existing["status"],
            "filename": existing["filename"],
            "duplicate": True,
            "size": existing.get("size", size),
        }
    created = await asyncio.to_thread(
        store.create_document,
        space_id,
        owner_id,
        filename,
        str(target_path),
        file.content_type,
        document_id=document_id,
        size=size,
        content_hash=content_hash,
    )
    return {**created, "size": size}


@app.get("/v1/documents/{document_id}", dependencies=[Depends(require_api_key)])
async def get_document(document_id: str, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    owner_id, is_admin = scope
    document = await asyncio.to_thread(store.get_document, document_id, owner_id, is_admin)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    return document


@app.get("/v1/documents/{document_id}/stats", dependencies=[Depends(require_api_key)])
async def get_document_stats(document_id: str, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    owner_id, is_admin = scope
    stats = await asyncio.to_thread(store.get_document_stats, document_id, owner_id, is_admin)
    if not stats:
        raise HTTPException(status_code=404, detail="Document not found")
    return stats


@app.post("/v1/documents/{document_id}/reprocess", dependencies=[Depends(require_api_key)])
async def reprocess_document(document_id: str, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    """Requeue an existing document so improved parsers can rebuild its index."""
    owner_id, is_admin = scope
    queued = await asyncio.to_thread(store.requeue_document, document_id, owner_id, is_admin)
    if not queued:
        raise HTTPException(status_code=404, detail="Document or processing job not found")
    return queued


@app.post("/v1/documents/parse", dependencies=[Depends(require_api_key)])
async def parse_uploaded_file(file: UploadFile = File(...)) -> dict[str, Any]:
    """Parse an uploaded file and return extracted text."""
    suffix = Path(file.filename or "").suffix.lower()
    supported = {".pdf", ".doc", ".docx", ".txt", ".md", ".csv", ".xlsx", ".xls", ".json"}
    if suffix not in supported:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {suffix}")
    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (max 50MB)")
    with tempfile.TemporaryDirectory(prefix="huizhi-parse-") as tmp_dir:
        tmp_path = Path(tmp_dir) / (file.filename or f"upload{suffix}")
        tmp_path.write_bytes(data)
        try:
            result = extract_text_from_file(
                tmp_path,
                settings.parser_backend,
                settings.mineru_command,
                offline=settings.knowledge_offline,
                mineru_timeout_seconds=settings.mineru_timeout_seconds,
            )
        except Exception as exc:
            logger.exception("document parse failed: filename=%s", file.filename)
            raise HTTPException(status_code=500, detail=f"Parse failed: {exc}")
    return result


@app.delete("/v1/documents/{document_id}", dependencies=[Depends(require_api_key)])
async def delete_document(document_id: str, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    """Delete a document and all its related data."""
    owner_id, is_admin = scope
    # Read the doc before deleting: graph cleanup needs its space and original
    # filename to reconstruct the LightRAG document id (which is derived from
    # a space-scoped source ref — see lightrag_bridge).
    document = await asyncio.to_thread(store.get_document, document_id, owner_id, is_admin)
    result = await asyncio.to_thread(store.delete_document, document_id, owner_id, is_admin)
    if result == "not_found":
        raise HTTPException(status_code=404, detail="Document not found")
    if result == "processing":
        raise HTTPException(status_code=409, detail="Document is being processed, please try again later")
    # Local PostgreSQL is authoritative; graph cleanup is best-effort and
    # must not make a successfully deleted document appear to fail.
    if document:
        space_id = document.get("space_id") or ""
        source_name = document.get("filename") or ""
        asyncio.create_task(
            lightrag.delete_document(space_id, document_id, source_name),
        )
    return {"deleted": True}


@app.get("/v1/documents/{document_id}/file", dependencies=[Depends(require_api_key)])
async def get_document_file(document_id: str, scope: tuple[str, bool] = Depends(request_scope)) -> FileResponse:
    """返回原始文件，支持PDF/DOCX/TXT等格式的带格式预览"""
    owner_id, is_admin = scope
    document = await asyncio.to_thread(store.get_document, document_id, owner_id, is_admin)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    file_path = Path(document["path"])
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")
    media_type = document.get("media_type") or "application/octet-stream"
    return FileResponse(path=str(file_path), media_type=media_type, filename=document["filename"])


@app.get("/v1/documents", dependencies=[Depends(require_api_key)])
async def list_all_documents(
    filename: str = Query(default=""),
    created_after: str = Query(default=""),
    created_before: str = Query(default=""),
    doc_ids: str = Query(default=""),
    status: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    scope: tuple[str, bool] = Depends(request_scope),
) -> dict[str, Any]:
    owner_id, is_admin = scope
    ids = [x.strip() for x in doc_ids.split(",") if x.strip()] if doc_ids else None
    valid_statuses = {"queued", "processing", "completed", "failed"}
    if status and status not in valid_statuses:
        raise HTTPException(status_code=422, detail="Unsupported document status")
    items, total = await asyncio.to_thread(
        store.list_user_documents_page,
        owner_id,
        is_admin,
        filename_like=filename or None,
        created_after=created_after or None,
        created_before=created_before or None,
        doc_ids=ids,
        status=status or None,
        page=page,
        page_size=page_size,
    )
    pending_count = await asyncio.to_thread(store.count_pending_jobs, owner_id)
    return {"items": items, "total": total, "page": page, "page_size": page_size, "pending_count": pending_count}


@app.get("/v1/jobs", dependencies=[Depends(require_api_key)])
async def list_all_jobs(
    scope: tuple[str, bool] = Depends(request_scope),
) -> list[dict[str, Any]]:
    owner_id, is_admin = scope
    return await asyncio.to_thread(store.list_user_jobs, owner_id, is_admin)


@app.get("/v1/documents/{document_id}/preview", dependencies=[Depends(require_api_key)])
async def preview_document(
    document_id: str, scope: tuple[str, bool] = Depends(request_scope)
) -> dict[str, Any]:
    owner_id, is_admin = scope
    document = await asyncio.to_thread(store.get_document, document_id, owner_id, is_admin)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    file_path = Path(document["path"])
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")
    suffix = file_path.suffix.lower()
    MAX_LINES = 100
    content = ""
    if suffix in (".txt", ".md", ".json", ".csv"):
        content = file_path.read_text(encoding="utf-8", errors="replace")[: MAX_LINES * 200]
    elif suffix in (".xls", ".xlsx", ".xlsm"):
        try:
            import pandas as pd

            df = pd.read_excel(str(file_path), nrows=MAX_LINES)
            content = df.to_string(index=False)
        except Exception as e:
            content = f"[Excel预览失败: {e}]"
    elif suffix == ".pdf":
        content = "[PDF预览：请使用原文件查看，或通过 chunks 接口查看内容片段]"
    elif suffix in (".doc", ".docx"):
        content = "[DOCX预览：请使用原文件查看，或通过 chunks 接口查看内容片段]"
    else:
        content = file_path.read_text(encoding="utf-8", errors="replace")[: MAX_LINES * 200]
    return {"document_id": document_id, "filename": document["filename"], "content": content, "truncated": True}


class FilterRequest(BaseModel):
    query: str = Field(min_length=2, max_length=100000)
    top_k: int = Field(default=20, ge=1, le=100)


@app.post("/v1/search/filter", dependencies=[Depends(require_api_key)])
async def search_filter(
    body: FilterRequest,
    scope: tuple[str, bool] = Depends(request_scope),
) -> dict[str, Any]:
    owner_id, is_admin = scope
    space_obj = await asyncio.to_thread(store.get_or_create_user_space, owner_id)
    space_id = space_obj["id"]
    evidence, _relations, _semantic = await _hybrid_search(space_id, body.query, body.top_k)
    doc_ids = list({e.get("document_id") for e in evidence if e.get("document_id")})
    return {"document_ids": doc_ids, "total_count": len(evidence), "document_count": len(doc_ids)}


@app.get("/v1/spaces/{space_id}/documents", dependencies=[Depends(require_api_key)])
async def list_documents(space_id: str, scope: tuple[str, bool] = Depends(request_scope)) -> list[dict[str, Any]]:
    owner_id, is_admin = scope
    if not await asyncio.to_thread(store.space_exists, space_id, owner_id, is_admin):
        raise HTTPException(status_code=404, detail="Knowledge space not found")
    return await asyncio.to_thread(store.list_documents, space_id, owner_id, is_admin)


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(require_api_key)])
async def get_job(job_id: str, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    owner_id, is_admin = scope
    job = await asyncio.to_thread(store.get_job, job_id, owner_id, is_admin)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/v1/jobs/{job_id}/log", dependencies=[Depends(require_api_key)])
async def get_job_log(job_id: str, scope: tuple[str, bool] = Depends(request_scope)) -> PlainTextResponse:
    owner_id, is_admin = scope
    log = await asyncio.to_thread(store.get_job_log, job_id, owner_id, is_admin)
    if log is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return PlainTextResponse(
        log,
        headers={"Content-Disposition": f'attachment; filename="job-{job_id}.log"'},
    )


async def _hybrid_search(space_id: str, query: str, top_k: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """融合检索：PostgreSQL 文件名/全文/字面/向量 + 实体上下文 + LightRAG 图检索。

    每条 evidence 附带 match_reasons（多路命中来源列表）、confidence（归一化置信度）、
    source（主来源 = 最高权重路径）。
    返回 (evidence, relations, semantic_enabled)。
    """
    local_evidence = await asyncio.to_thread(store.entity_context, space_id, query, top_k)
    direct = await asyncio.to_thread(store.search, space_id, query, top_k)
    by_filename = await asyncio.to_thread(store.search_by_filename, space_id, query, top_k)
    vector = await asyncio.to_thread(ModelClient(settings).embed, [query])
    semantic = (
        await asyncio.to_thread(
            store.search_by_embedding, space_id, vector[0], top_k, SEMANTIC_MIN_SCORE
        )
        if vector
        else []
    )

    # 聚合多路命中：按 chunk id 收集所有命中来源。
    # 文件名命中排在合并序列最前：merged 是插入序且后面直接被 top_k 截断，放在末尾会
    # 被内容命中挤掉，而「查询词只出现在文件名里」正是这一路要救的场景。文件名命中
    # 只在查询词确实出现在文件名时就产生，精度高于内容/语义的模糊匹配，优先合理。
    merged: dict[str, dict[str, Any]] = {}
    for item in [*by_filename, *local_evidence, *direct, *semantic]:
        key = item.get("id") or f"{item.get('document_id')}:{item.get('page')}:{item.get('location')}"
        source = item.pop("source", "unknown")
        if key in merged:
            existing = merged[key]
            if source not in existing["match_reasons"]:
                existing["match_reasons"].append(source)
            existing_score = existing.get("score") or 0
            new_score = item.get("score") or 0
            if new_score > existing_score:
                existing["score"] = new_score
        else:
            item["match_reasons"] = [source]
            merged[key] = item

    # 归一化置信度：多路命中 = 高置信；单路按来源权重分档
    SOURCE_WEIGHT = {
        "fulltext": 1.0,
        "filename": 0.95,
        "literal": 0.9,
        "semantic": 0.8,
        "entity": 0.7,
    }
    for item in merged.values():
        reasons = item.get("match_reasons", [])
        if len(reasons) >= 2:
            item["confidence"] = "high"
        else:
            weight = SOURCE_WEIGHT.get(reasons[0], 0.5)
            item["confidence"] = "high" if weight >= 0.9 else "medium" if weight >= 0.7 else "low"
        item["source"] = max(reasons, key=lambda r: SOURCE_WEIGHT.get(r, 0))

    # LightRAG 图检索：本地关键词匹配对语义改写和跨文档关联较弱，
    # 为图谱证据保留约 1/3 配额，避免被低质量字面匹配挤占。
    graph_results = []
    if merged:
        graph_results = await lightrag.search(query, space_id, top_k)
    if graph_results:
        for g in graph_results:
            g["match_reasons"] = [g.get("source", "lightrag")]
            g["confidence"] = "medium"
        reserved = min(len(graph_results), max(2, top_k // 3))
        local_items = list(merged.values())
        evidence = [*local_items[:max(0, top_k - reserved)], *graph_results[:reserved]]
    else:
        evidence = list(merged.values())[:top_k]

    relations = await asyncio.to_thread(store.relation_context, space_id, query, top_k)
    return evidence, relations, bool(vector)


@app.post("/v1/query", dependencies=[Depends(require_api_key)])
async def query(body: QueryRequest, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    owner_id, is_admin = scope
    if not await asyncio.to_thread(store.space_exists, body.space_id, owner_id, is_admin):
        raise HTTPException(status_code=404, detail="Knowledge space not found")
    evidence, relations, semantic_enabled = await _hybrid_search(
        body.space_id, body.query, body.top_k
    )
    relations = relations if body.include_relations else []
    # Answers must be generated from the already space-filtered evidence. The
    # standalone LightRAG server has one shared graph and cannot safely answer
    # this request without a space filter.
    model_answer = await asyncio.to_thread(
        ModelClient(settings).answer, body.query, evidence, relations
    )
    return {
        "answer": model_answer,
        "evidence": evidence if body.include_evidence else [],
        "relations": relations,
        "semantic_search_enabled": semantic_enabled,
        "lightrag": {"enabled": lightrag.enabled},
    }


@app.get("/v1/evidence/{evidence_id}", dependencies=[Depends(require_api_key)])
async def evidence(evidence_id: str, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    item = await asyncio.to_thread(store.get_evidence, evidence_id)
    if not item:
        raise HTTPException(status_code=404, detail="Evidence not found")
    owner_id, is_admin = scope
    document = await asyncio.to_thread(store.get_document, item["document_id"], owner_id, is_admin)
    if not document:
        raise HTTPException(status_code=404, detail="Evidence not found")
    return item


@app.post("/v1/query/stream", dependencies=[Depends(require_api_key)])
async def query_stream(body: QueryRequest, scope: tuple[str, bool] = Depends(request_scope)) -> StreamingResponse:
    """流式查询接口，返回SSE格式的流式响应"""
    owner_id, is_admin = scope
    if not await asyncio.to_thread(store.space_exists, body.space_id, owner_id, is_admin):
        raise HTTPException(status_code=404, detail="Knowledge space not found")

    # 融合检索（本地 + LightRAG 图检索）
    evidence, relations, _ = await _hybrid_search(body.space_id, body.query, body.top_k)
    relations = relations if body.include_relations else []

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
    if not await asyncio.to_thread(store.space_exists, body.space_id, owner_id, is_admin):
        raise HTTPException(status_code=404, detail="Knowledge space not found")

    # 融合检索（本地 + LightRAG 图检索）
    evidence, relations, semantic_enabled = await _hybrid_search(
        body.space_id, body.query, body.top_k
    )
    relations = relations if body.include_relations else []

    # 按分数过滤
    if body.min_score > 0:
        evidence = [item for item in evidence if (item.get("score") or 0) >= body.min_score]

    # 按文档分组统计（LightRAG 片段无 document_id，归入 graph 组）
    doc_groups: dict[str, dict[str, Any]] = {}
    for item in evidence:
        doc_id = item.get("document_id") or "lightrag-graph"
        if doc_id not in doc_groups:
            doc_groups[doc_id] = {
                "document_id": doc_id,
                "filename": item.get("filename") or ("LightRAG 图检索" if doc_id == "lightrag-graph" else None),
                "snippet_count": 0,
                "pages": [],
                "sources": [],
            }
        doc_groups[doc_id]["snippet_count"] += 1
        page = item.get("page")
        if page and page not in doc_groups[doc_id]["pages"]:
            doc_groups[doc_id]["pages"].append(page)
        source = item.get("source", "unknown")
        if source not in doc_groups[doc_id]["sources"]:
            doc_groups[doc_id]["sources"].append(source)

    return {
        "query": body.query,
        "evidence": evidence,
        "documents": list(doc_groups.values()),
        "relations": relations,
        "total_count": len(evidence),
        "document_count": len(doc_groups),
        "semantic_search_enabled": semantic_enabled,
    }


@app.post("/v1/documents/chunks", dependencies=[Depends(require_api_key)])
async def get_document_chunks(body: DocumentDetailRequest, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    """获取指定文档的详细片段"""
    owner_id, is_admin = scope
    document = await asyncio.to_thread(store.get_document, body.document_id, owner_id, is_admin)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    # 获取文档的所有evidence
    chunks = await asyncio.to_thread(store.get_document_evidence, body.document_id)

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
    document = await asyncio.to_thread(store.get_document, document_id, owner_id, is_admin)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    evidence = await asyncio.to_thread(store.get_document_evidence, document_id)
    if page is not None:
        evidence = [e for e in evidence if e.get("page") == page]

    return {
        "document_id": document_id,
        "filename": document.get("filename"),
        "evidence": evidence[:limit],
        "total_count": len(evidence),
    }


class ReindexRequest(BaseModel):
    space_id: str


@app.post("/v1/lightrag/reindex", dependencies=[Depends(require_api_key)])
async def reindex_lightrag(body: ReindexRequest, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    """将指定知识空间的所有已完成文档重新索引到 LightRAG 图谱。"""
    owner_id, is_admin = scope
    if not await asyncio.to_thread(store.space_exists, body.space_id, owner_id, is_admin):
        raise HTTPException(status_code=404, detail="Knowledge space not found")
    if not lightrag.enabled:
        raise HTTPException(status_code=503, detail="LightRAG is not configured (LIGHTRAG_URL not set)")
    result = await lightrag.reindex_space(store, body.space_id)
    return {"space_id": body.space_id, **result}


@app.post("/v1/lightrag/migrate", dependencies=[Depends(require_api_key)])
async def migrate_lightrag(body: ReindexRequest, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    """把指定知识空间的历史图谱记录迁移到空间作用域命名。

    升级前的来源标识是裸文件名，LightRAG 存储后不含空间信息，当前按空间过滤时
    全部落选（图谱证据未接入）。本接口逐文档「先删旧记录、再按新命名重建」。

    幂等，可重复调用：已完成迁移的文档重建时会命中 LLM 缓存与内容一致判定，
    不会重复消耗抽取算力。逐文档隔离失败，失败的会出现在 details 里，可重跑。
    """
    owner_id, is_admin = scope
    if not await asyncio.to_thread(store.space_exists, body.space_id, owner_id, is_admin):
        raise HTTPException(status_code=404, detail="Knowledge space not found")
    if not lightrag.enabled:
        raise HTTPException(status_code=503, detail="LightRAG is not configured (LIGHTRAG_URL not set)")
    return await lightrag.migrate_legacy_documents(store, body.space_id)
