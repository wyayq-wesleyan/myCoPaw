from __future__ import annotations

from typing import Any

import httpx

from .config import Settings


class LightRAGBridge:
    """Bridge to a separately deployed LightRAG server.

    Enhances retrieval with LightRAG's graph-aware query API.
    Local PostgreSQL remains the authoritative metadata/evidence store.
    LightRAG provides graph-based semantic search as a complementary signal.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.lightrag_url)

    async def query(self, query: str) -> dict[str, Any] | None:
        """Query LightRAG for graph-aware answer (full response)."""
        if not self.enabled:
            return None
        headers = {"X-API-Key": self.settings.lightrag_api_key} if self.settings.lightrag_api_key else {}
        payload = {
            "query": query,
            "mode": self.settings.lightrag_query_mode,
            "include_references": True,
            "include_chunk_content": True,
        }
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                response = await client.post(f"{self.settings.lightrag_url}/query", headers=headers, json=payload)
                response.raise_for_status()
                return response.json()
        except Exception:
            return None

    async def search(self, query: str, top_k: int = 20) -> list[dict[str, Any]]:
        """Query LightRAG retrieval data (no LLM generation) for fusion with local search."""
        if not self.enabled:
            return []
        headers = {"X-API-Key": self.settings.lightrag_api_key} if self.settings.lightrag_api_key else {}
        payload = {
            "query": query,
            "mode": self.settings.lightrag_query_mode,
        }
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(f"{self.settings.lightrag_url}/query/data", headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
                return self._normalize_lightrag_result(data)
        except Exception:
            return []

    def _normalize_lightrag_result(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Convert LightRAG /query/data response to standard evidence format."""
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return []
        evidence: list[dict[str, Any]] = []
        for index, chunk in enumerate(data.get("chunks") or []):
            content = chunk.get("content") or chunk.get("text") or ""
            if not content:
                continue
            evidence.append({
                "id": f"lightrag-{chunk.get('chunk_id') or index}",
                "document_id": None,
                "page": None,
                "location": str(chunk.get("file_path") or "LightRAG 图谱"),
                "content": content,
                "filename": chunk.get("file_path"),
                "source": "lightrag",
                "score": 0.5,
            })
        return evidence

    async def insert(self, source_name: str, chunks: list[dict[str, Any]]) -> bool:
        """Index normalized content in LightRAG after local evidence is durable."""
        if not self.enabled or not chunks:
            return False
        headers = {"X-API-Key": self.settings.lightrag_api_key} if self.settings.lightrag_api_key else {}
        text = "\n\n".join(
            f"[{chunk.get('location') or '正文'} | 第 {chunk.get('page') or '?'} 页]\n{chunk['content']}"
            for chunk in chunks
        )
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    f"{self.settings.lightrag_url}/documents/text",
                    headers=headers,
                    json={"text": text, "file_source": source_name},
                )
                # 409 = 内容已在图谱中，视为索引成功
                if response.status_code == 409:
                    return True
                response.raise_for_status()
                return True
        except Exception:
            return False

    async def reindex_space(self, store, space_id: str) -> dict[str, int]:
        """Reindex all completed documents in a space to LightRAG."""
        if not self.enabled:
            return {"indexed": 0, "failed": 0}

        docs = store.list_documents(space_id, owner_id="", is_admin=True)
        indexed = 0
        failed = 0
        for doc in docs:
            if doc.get("status") != "completed":
                continue
            chunks = store.get_document_evidence(doc["id"])
            if chunks and await self.insert(doc["filename"], chunks):
                indexed += 1
            else:
                failed += 1
        return {"indexed": indexed, "failed": failed}

    async def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.settings.lightrag_url}/health")
                return {"enabled": True, "reachable": response.is_success, "status_code": response.status_code}
        except Exception as exc:
            return {"enabled": True, "reachable": False, "error": str(exc)}