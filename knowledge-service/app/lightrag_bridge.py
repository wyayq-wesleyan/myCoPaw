from __future__ import annotations

from typing import Any

import httpx

from .config import Settings


class LightRAGBridge:
    """Optional bridge to a separately deployed LightRAG server.

    The local SQLite index remains the authoritative metadata/evidence store.
    When LIGHTRAG_URL is set, the bridge augments retrieval with LightRAG's
    graph-aware query API without exposing LightRAG directly to the assistant.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.lightrag_url)

    async def query(self, query: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        headers = {"Authorization": f"Bearer {self.settings.lightrag_api_key}"} if self.settings.lightrag_api_key else {}
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

    async def insert(self, source_name: str, chunks: list[dict[str, Any]]) -> bool:
        """Index normalized content in LightRAG after local evidence is durable.

        The local SQLite store is written first and remains available when the
        optional graph index is temporarily offline. A later reindex command
        can safely replay the normalized source if needed.
        """
        if not self.enabled or not chunks:
            return False
        headers = {"Authorization": f"Bearer {self.settings.lightrag_api_key}"} if self.settings.lightrag_api_key else {}
        text = "\n\n".join(
            f"[{chunk.get('location') or '正文'} | 第 {chunk.get('page') or '?'} 页]\n{chunk['content']}"
            for chunk in chunks
        )
        try:
            async with httpx.AsyncClient(timeout=600) as client:
                response = await client.post(
                    f"{self.settings.lightrag_url}/documents/text",
                    headers=headers,
                    json={"text": text, "file_source": source_name},
                )
                response.raise_for_status()
                return True
        except Exception:
            return False

    async def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.settings.lightrag_url}/health")
                return {"enabled": True, "reachable": response.is_success, "status_code": response.status_code}
        except Exception as exc:
            return {"enabled": True, "reachable": False, "error": str(exc)}
