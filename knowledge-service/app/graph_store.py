# -*- coding: utf-8 -*-
"""Tenant-isolated graph storage: one LightRAG workspace per path.

Design: ``docs/知识服务多租户存储设计方案.md``.

**Why this exists.** The legacy arrangement pointed the knowledge service at a
single shared LightRAG server and narrowed results afterwards by parsing a
space/document prefix out of the source filename. That is read-then-filter: every
entity and relation from every case lands in one graph, so (a) any read path that
forgets the filter leaks, and (b) *LightRAG merges same-named entities across
cases*, which corrupts inference — exactly the "conflicting data confuses the
agent" problem the isolation was meant to prevent.

**What replaces it.** Each path gets its own LightRAG ``workspace``, and every
LightRAG table carries ``workspace`` **in its primary key** (verified: all 13
tables, e.g. ``lightrag_doc_full(workspace,id)``,
``lightrag_graph_nodes(workspace,namespace,id)``). Isolation is therefore applied
by the database when the row is read, not by Python after the fact.

**Honest scope note.** This is stronger than prefix filtering but weaker than one
database per tenant: it still depends on the caller passing the right workspace
list. The single source of that list is the main app's
``agent_path_ids`` → workspace mapping; nothing else constructs it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Prefix keeps a path-derived workspace distinguishable from LightRAG's own
# default ("") and makes the mapping obvious in the database.
WORKSPACE_PREFIX = "ws_"

# LightRAG table names are fixed (they do not vary by workspace or schema), so the
# physical boundary between "business data" and "graph data" is the database, not
# a schema. See the design doc §5.
_REQUIRED_STORAGES = (
    "PGKVStorage",
    "PGVectorStorage",
    "PGTableGraphStorage",  # not PGGraphStorage: that one requires Apache AGE
    "PGDocStatusStorage",
)


def workspace_for_path(path_id: str) -> str:
    """Map a path id to its LightRAG workspace name.

    The path id is a UUID; stripping the dashes leaves only ``[0-9a-f]``, which
    satisfies LightRAG's ``validate_workspace`` (no ``/``, ``\\``, ``.``, ``..``)
    without escaping. Fixed length 35 keeps it indexable and easy to eyeball.

    Mirrors the data-space schema naming (``p_<same 32 hex>``) so one path is
    recognisable in both stores.
    """
    compact = str(path_id or "").lower().replace("-", "").strip()
    if not compact or any(c not in "0123456789abcdef" for c in compact):
        raise ValueError(f"路径 id 不是有效的 UUID：{path_id!r}")
    return f"{WORKSPACE_PREFIX}{compact}"


class GraphStoreUnavailable(RuntimeError):
    """The in-process graph store could not be used.

    Callers must treat this as **no access** rather than falling back to an
    unfiltered path: a graph outage must not remove the isolation boundary.
    """


class GraphPool:
    """Lazily creates and caches one in-process LightRAG per workspace.

    Connections are bounded by **one** knob, not two. LightRAG keeps a single
    process-wide PostgreSQL pool (``ClientManager`` is a class-level singleton)
    shared by every workspace's storages, sized by ``graph_pool_max_connections``.
    A process therefore uses at most that many graph connections however many
    workspaces exist; ``max_workspaces`` only caps how many LightRAG instances
    stay resident (memory) before LRU eviction.

    An earlier version of this docstring claimed ``max_workspaces x
    pool_max_connections``. That overstated the budget ~32x and is the kind of
    number an operator sizes ``max_connections`` against, so it is corrected here
    rather than left as folklore.

    A workspace is created only when first used — with one path per case there can
    be many paths but only a few active at a time, so eager construction would
    waste both memory and connections.
    """

    def __init__(self, settings) -> None:
        self._settings = settings
        self._instances: "OrderedDict[str, object]" = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    # --- configuration -----------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Whether tenant-isolated graph storage is configured."""
        return bool(getattr(self._settings, "graph_database_url", ""))

    def _dsn_parts(self) -> dict[str, str]:
        parsed = urlparse(self._settings.graph_database_url)
        if not parsed.hostname or not parsed.path:
            raise GraphStoreUnavailable(
                f"KNOWLEDGE_GRAPH_DATABASE_URL 无法解析：{self._settings.graph_database_url!r}",
            )
        return {
            "POSTGRES_HOST": parsed.hostname,
            "POSTGRES_PORT": str(parsed.port or 5432),
            "POSTGRES_USER": parsed.username or "",
            "POSTGRES_PASSWORD": parsed.password or "",
            "POSTGRES_DATABASE": parsed.path.lstrip("/"),
            "POSTGRES_MAX_CONNECTIONS": str(
                getattr(self._settings, "graph_pool_max_connections", 4)
            ),
            # The graph tables are ordinary tables + indexes (PGTableGraphStorage),
            # so no extension is required. Set explicitly to avoid inheriting a
            # stale value from the environment.
            "POSTGRES_VECTOR_INDEX_TYPE": "HNSW",
            "POSTGRES_WORKSPACE": "",
        }

    def _embedding_func(self):
        """LightRAG's embedding adapter, wired to the configured model.

        Reuses the same endpoint the rest of the service uses, so graph vectors and
        chunk vectors cannot drift to different models/dimensions.
        """
        import numpy as np
        from lightrag.utils import EmbeddingFunc

        import httpx

        settings = self._settings
        dim = int(settings.embedding_dimensions or 1024)

        async def _embed(texts, **kwargs):
            payload = {"model": settings.embedding_model, "input": list(texts)}
            headers = (
                {"Authorization": f"Bearer {settings.embedding_api_key}"}
                if settings.embedding_api_key
                else {}
            )
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    f"{settings.embedding_base_url.rstrip('/')}/embeddings",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                data = response.json().get("data", [])
            vectors = [item["embedding"] for item in data]
            return np.asarray(vectors, dtype=np.float32)

        return EmbeddingFunc(embedding_dim=dim, max_token_size=8192, func=_embed)

    def _llm_func(self):
        """LightRAG's LLM adapter, wired to the configured chat model."""
        import httpx

        settings = self._settings

        async def _complete(prompt, system_prompt=None, history_messages=None, **kwargs):
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.extend(history_messages or [])
            messages.append({"role": "user", "content": prompt})
            headers = (
                {"Authorization": f"Bearer {settings.chat_api_key}"}
                if settings.chat_api_key
                else {}
            )
            async with httpx.AsyncClient(timeout=300) as client:
                response = await client.post(
                    f"{settings.chat_base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json={"model": settings.chat_model, "messages": messages},
                )
                response.raise_for_status()
                return response.json()["choices"][0]["message"]["content"]

        return _complete

    # --- lifecycle ---------------------------------------------------------

    async def get(self, workspace: str):
        """Return the LightRAG instance for *workspace*, creating it if needed."""
        if not self.enabled:
            raise GraphStoreUnavailable("图谱存储未启用（未配置 KNOWLEDGE_GRAPH_DATABASE_URL）")

        async with self._guard:
            cached = self._instances.get(workspace)
            if cached is not None:
                self._instances.move_to_end(workspace)
                return cached
            lock = self._locks.setdefault(workspace, asyncio.Lock())

        # Built outside the global guard: construction opens connections and must
        # not serialize every other workspace behind it.
        async with lock:
            async with self._guard:
                cached = self._instances.get(workspace)
                if cached is not None:
                    return cached
            instance = await self._build(workspace)
            async with self._guard:
                self._instances[workspace] = instance
                self._instances.move_to_end(workspace)
                await self._evict_if_needed()
            return instance

    async def _build(self, workspace: str):
        from lightrag import LightRAG

        for key, value in self._dsn_parts().items():
            os.environ[key] = value

        rag = LightRAG(
            working_dir=f"{self._settings.data_dir}/graph/{workspace}",
            workspace=workspace,
            kv_storage="PGKVStorage",
            vector_storage="PGVectorStorage",
            graph_storage="PGTableGraphStorage",
            doc_status_storage="PGDocStatusStorage",
            llm_model_func=self._llm_func(),
            embedding_func=self._embedding_func(),
        )
        await rag.initialize_storages()
        logger.info("graph: workspace %s ready", workspace)
        return rag

    async def _evict_if_needed(self) -> None:
        """Drop the least-recently-used instances beyond the ceiling.

        Eviction closes only the storage handles; data lives in PostgreSQL, so the
        next access rebuilds the instance and reads everything back.
        """
        limit = int(getattr(self._settings, "graph_max_workspaces", 32) or 32)
        while len(self._instances) > limit:
            workspace, instance = self._instances.popitem(last=False)
            try:
                await instance.finalize_storages()
            except Exception:  # noqa: BLE001 - eviction must not break the caller
                logger.warning("graph: could not finalize %s", workspace, exc_info=True)
            logger.info("graph: evicted workspace %s (LRU)", workspace)

    async def close(self) -> None:
        """Release every instance (shutdown)."""
        async with self._guard:
            instances = list(self._instances.items())
            self._instances.clear()
        for workspace, instance in instances:
            try:
                await instance.finalize_storages()
            except Exception:  # noqa: BLE001
                logger.warning("graph: could not finalize %s", workspace, exc_info=True)


class TenantGraph:
    """Workspace-scoped insert / search / delete on top of :class:`GraphPool`.

    This is the only place that reaches into LightRAG for tenant data, so the
    isolation rule has one implementation: every call names the workspace(s) it may
    touch, and nothing here ever supplies a workspace the caller did not pass.
    """

    def __init__(self, pool: GraphPool) -> None:
        self._pool = pool

    @property
    def enabled(self) -> bool:
        return self._pool.enabled

    async def insert(
        self,
        *,
        workspace: str,
        document_id: str,
        filename: str,
        chunks: list[dict],
    ) -> bool:
        """Index one document into its workspace.

        ``file_path`` is a stable per-document value so the document can be found
        and deleted later. It no longer encodes the space (the workspace column does
        that), which also removes the old normalisation trap where LightRAG stripped
        any directory component and a path-style key silently matched nothing.

        **Returns whether the document is actually usable, not merely whether the
        call returned.** LightRAG records a *failed* document status when extraction
        times out or the model errors, and ``ainsert`` still returns normally — so
        trusting the call alone reported success for documents whose graph is empty.
        A migration built on that would claim "0 failed" while indexing nothing,
        which is precisely the kind of silent lie that makes a rebuild untrustworthy.
        """
        if not self.enabled or not chunks:
            return False
        text = "\n\n".join(
            f"[{c.get('location') or '正文'} | 第 {c.get('page') or '?'} 页]\n{c['content']}"
            for c in chunks
            if c.get("content")
        )
        if not text:
            return False
        source_ref = f"{document_id}__{filename}"
        try:
            rag = await self._pool.get(workspace)
            await rag.ainsert(text, file_paths=source_ref)
        except Exception:  # noqa: BLE001 - indexing must not lose the document
            logger.warning(
                "graph: indexing failed workspace=%s doc=%s",
                workspace, document_id, exc_info=True,
            )
            return False
        return await self._is_usable(workspace, source_ref)

    async def _is_usable(self, workspace: str, source_ref: str) -> bool:
        """Whether a document finished processing (not failed/pending).

        Extracted so ``insert`` and ``migrate_legacy`` judge success the same way.
        """
        try:
            rag = await self._pool.get(workspace)
            record = await rag.doc_status.get_doc_by_file_path(source_ref)
        except Exception:  # noqa: BLE001
            logger.warning("graph: could not verify %s", source_ref, exc_info=True)
            return False
        if not record:
            return False
        status = record.get("status") if isinstance(record, dict) else getattr(record, "status", None)
        status = str(getattr(status, "value", status) or "")
        if status and status != "processed":
            error = record.get("error_msg") if isinstance(record, dict) else getattr(record, "error_msg", None)
            logger.warning(
                "graph: document not usable workspace=%s ref=%s status=%s error=%s",
                workspace, source_ref, status, str(error)[:200],
            )
            return False
        return True

    async def _find_doc_key(self, workspace: str, document_id: str) -> str | None:
        """LightRAG's own doc id (``doc-<md5>``) for our ``<id>__<name>`` marker.

        Delete and existence checks need LightRAG's id, not the file path we write:
        passing the path silently matched nothing while still reporting success.
        """
        try:
            rag = await self._pool.get(workspace)
            page = await rag.doc_status.get_docs_paginated(page=1, page_size=1000)
            rows = page[0] if isinstance(page, tuple) else page
            prefix = f"{document_id}__"
            for row in rows or []:
                key = row[0] if isinstance(row, (tuple, list)) and row else None
                status = row[1] if isinstance(row, (tuple, list)) and len(row) > 1 else row
                path = (
                    status.get("file_path") if isinstance(status, dict)
                    else getattr(status, "file_path", None)
                )
                if str(path or "").startswith(prefix):
                    return str(key) if key else None
            return None
        except Exception:  # noqa: BLE001
            logger.warning(
                "graph: doc key lookup failed ws=%s doc=%s",
                workspace, document_id, exc_info=True,
            )
            return None

    async def _delete_docs(self, workspace: str, document_id: str) -> int:
        """Delete every graph row for one document; returns how many were removed."""
        try:
            rag = await self._pool.get(workspace)
            page = await rag.doc_status.get_docs_paginated(page=1, page_size=1000)
            rows = page[0] if isinstance(page, tuple) else page
            prefix = f"{document_id}__"
            removed = 0
            for row in rows or []:
                key = row[0] if isinstance(row, (tuple, list)) and row else None
                status = row[1] if isinstance(row, (tuple, list)) and len(row) > 1 else row
                path = (
                    status.get("file_path") if isinstance(status, dict)
                    else getattr(status, "file_path", None)
                )
                if str(path or "").startswith(prefix) and key:
                    await rag.adelete_by_doc_id(str(key))
                    removed += 1
            return removed
        except Exception:  # noqa: BLE001
            logger.warning(
                "graph: delete failed ws=%s doc=%s", workspace, document_id, exc_info=True,
            )
            return 0

    async def search(
        self,
        *,
        query: str,
        workspaces: list[str],
        top_k: int = 20,
        mode: str = "mix",
    ) -> list[dict]:
        """Retrieve graph evidence from **only** the given workspaces.

        Each workspace is queried on its own and the results are merged, rather
        than querying one combined graph: merging graphs would let a same-named
        entity in case A link to case B's material, which is the pollution this
        design exists to prevent.

        An empty list means "no access" and returns nothing — never "all".
        """
        if not self.enabled or not workspaces:
            return []

        async def one(workspace: str) -> list[dict]:
            try:
                rag = await self._pool.get(workspace)
                # ``aquery_data`` requires a QueryParam instance; passing a dict
                # fails inside LightRAG with "'dict' object has no attribute 'mode'".
                # It lives in ``lightrag.base`` in 1.5.6 (not ``utils``/``types``).
                from lightrag.base import QueryParam

                payload = await rag.aquery_data(
                    query,
                    param=QueryParam(mode=mode, only_need_context=True),
                )
                return self._to_evidence(payload, workspace)
            except Exception:  # noqa: BLE001 - one workspace must not fail the rest
                logger.warning("graph: search failed workspace=%s", workspace, exc_info=True)
                return []

        batches = await asyncio.gather(*(one(ws) for ws in workspaces))
        return self._merge(batches, top_k)

    @staticmethod
    def _to_evidence(payload, workspace: str) -> list[dict]:
        """Normalise LightRAG's ``aquery_data`` payload into evidence rows."""
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return []
        out: list[dict] = []
        for index, chunk in enumerate(data.get("chunks") or []):
            content = chunk.get("content") or chunk.get("text") or ""
            if not content:
                continue
            file_path = str(chunk.get("file_path") or "")
            # ``file_path`` is "<document_id>__<filename>"; recover the id so the
            # caller can attribute the evidence (and so it is traceable to a file).
            document_id = file_path.split("__", 1)[0] if "__" in file_path else None
            out.append({
                "id": f"lightrag-{workspace}-{chunk.get('chunk_id') or index}",
                "document_id": document_id,
                "page": None,
                "location": file_path or "LightRAG 图谱",
                "content": content,
                "filename": file_path.split("__", 1)[1] if "__" in file_path else file_path,
                "source": "lightrag",
                "score": float(chunk.get("score") or 0.5),
                "workspace": workspace,
            })
        return out

    @staticmethod
    def _merge(batches: list[list[dict]], top_k: int) -> list[dict]:
        """Merge per-workspace evidence by normalised score.

        Scores come from different workspaces and are not directly comparable, so
        each batch is normalised against its own top score first. Without this the
        workspace that happens to produce larger absolute numbers floods the
        result and the others effectively disappear.
        """
        merged: list[dict] = []
        seen: set[str] = set()
        for batch in batches:
            if not batch:
                continue
            top = max((item.get("score") or 0.0) for item in batch) or 1.0
            for item in batch:
                key = f"{item.get('workspace')}::{item.get('id')}"
                if key in seen:
                    continue
                seen.add(key)
                item = dict(item)
                item["score"] = (item.get("score") or 0.0) / top
                merged.append(item)
        merged.sort(key=lambda i: i.get("score") or 0.0, reverse=True)
        return merged[:top_k]

    async def delete(self, *, workspace: str, document_id: str, filename: str) -> bool:
        """Remove one document's graph data from its workspace.

        Reports whether anything was actually removed: a no-op that returns "ok"
        would let a caller believe cleanup succeeded while the rows (and their
        entity/relation links) remained.
        """
        if not self.enabled:
            return False
        removed = await self._delete_docs(workspace, document_id)
        if removed == 0:
            logger.info("graph: nothing to delete ws=%s doc=%s", workspace, document_id)
        return removed > 0

    async def count_documents(self, workspace: str) -> int:
        """How many documents this workspace holds (used by migration checks).

        ``get_all_status_counts`` returns a per-status breakdown *plus* an ``all``
        total; summing the values would double-count because ``all`` already
        includes the individual statuses.
        """
        if not self.enabled:
            return 0
        try:
            rag = await self._pool.get(workspace)
            counts = await rag.doc_status.get_all_status_counts() or {}
            if isinstance(counts, dict) and "all" in counts:
                return int(counts["all"])
            return sum(int(v) for v in counts.values()) if isinstance(counts, dict) else 0
        except Exception:  # noqa: BLE001
            logger.warning("graph: count failed workspace=%s", workspace, exc_info=True)
            return 0

    async def has_document(self, workspace: str, document_id: str) -> bool:
        """Whether one document is present in *workspace*.

        The migration's idempotency check: a rerun must skip completed work rather
        than duplicate it.
        """
        if not self.enabled:
            return False
        return await self._find_doc_key(workspace, document_id) is not None

    async def is_usable(self, workspace: str, document_id: str, filename: str) -> bool:
        """Whether one document finished processing in *workspace*."""
        if not self.enabled:
            return False
        return await self._is_usable(workspace, f"{document_id}__{filename}")

    async def move(
        self,
        *,
        from_workspace: str,
        to_workspace: str,
        document_id: str,
        filename: str,
        chunks: list[dict],
    ) -> bool:
        """Move one document's graph data to another workspace.

        Implemented as **build-then-delete**, never the reverse: a graph cannot
        change workspace in place, and deleting first would destroy the only copy if
        the rebuild failed. Ordered this way, an interrupted move leaves the
        document readable in its old workspace — degraded, but never lost.

        Note the cost, which the UI must surface: this re-runs entity/relation
        extraction, so it is *not* the metadata-only operation that moving a data
        table is.
        """
        if not self.enabled:
            return False
        if from_workspace == to_workspace:
            return True
        # Build first and confirm it is usable; only then remove the source. If the
        # rebuild fails (extraction timeout, model error) the document stays
        # readable in its old workspace — degraded, never lost.
        indexed = await self.insert(
            workspace=to_workspace,
            document_id=document_id,
            filename=filename,
            chunks=chunks,
        )
        if not indexed:
            return False
        await self.delete(
            workspace=from_workspace,
            document_id=document_id,
            filename=filename,
        )
        return True

    async def migrate_legacy(
        self,
        *,
        targets: list[dict],
        chunk_loader,
        on_progress=None,
    ) -> dict:
        """Rebuild legacy shared-graph documents into per-path workspaces.

        ``targets`` is a list of ``{document_id, filename, workspace}`` supplied by
        the caller (which owns the path model). ``chunk_loader(document_id)`` returns
        that document's chunks.

        Idempotent by construction: a document already present in its workspace is
        skipped, so a rerun after a partial failure only does the missing work. Each
        document is isolated — one failure does not abort the batch — and the report
        lists every failure for retry.

        **Why nothing is deleted from the legacy graph here.** The old shared graph
        is left untouched (and its files are kept) so the migration is reversible:
        switching the config back restores the previous behaviour. Cleanup of the
        legacy store is a separate, deliberate step after verification.
        """
        report = {"indexed": 0, "skipped": 0, "failed": 0, "details": []}
        for target in targets:
            document_id = target["document_id"]
            workspace = target["workspace"]
            try:
                if await self.has_document(workspace, document_id):
                    report["skipped"] += 1
                    if on_progress:
                        on_progress(document_id, "skipped")
                    continue
                chunks = chunk_loader(document_id)
                if not chunks:
                    report["failed"] += 1
                    report["details"].append(
                        {"document_id": document_id, "reason": "no chunks"},
                    )
                    continue
                ok = await self.insert(
                    workspace=workspace,
                    document_id=document_id,
                    filename=target["filename"],
                    chunks=chunks,
                )
                if ok:
                    report["indexed"] += 1
                    if on_progress:
                        on_progress(document_id, "indexed")
                else:
                    report["failed"] += 1
                    report["details"].append(
                        {"document_id": document_id, "reason": "insert failed"},
                    )
            except Exception as exc:  # noqa: BLE001 - isolate one document
                report["failed"] += 1
                report["details"].append(
                    {"document_id": document_id, "reason": str(exc)[:200]},
                )
                logger.warning(
                    "graph: migration failed doc=%s ws=%s",
                    document_id, workspace, exc_info=True,
                )
        return report


