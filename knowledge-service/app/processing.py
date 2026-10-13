from __future__ import annotations

import asyncio
from pathlib import Path
from collections.abc import Callable
from typing import Any

from .config import Settings
from .lightrag_bridge import LightRAGBridge
from .parsers import parse_document
from .providers import ModelClient
from .store import Store


def process_document(
    store: Store,
    settings: Settings,
    job: dict[str, Any],
    progress: Callable[[str], None] | None = None,
) -> None:
    def report(stage: str) -> None:
        if progress:
            progress(stage)

    document = store.get_document_by_id_for_worker(job["document_id"])
    if not document:
        raise RuntimeError(f"Document not found: {job['document_id']}")
    report("parsing")
    backend, chunks = parse_document(
        Path(document["path"]),
        settings.parser_backend,
        settings.mineru_command,
        offline=settings.knowledge_offline,
        mineru_timeout_seconds=settings.mineru_timeout_seconds,
    )
    for chunk in chunks:
        chunk["space_id"] = document["space_id"]
    client = ModelClient(settings)
    report("vision")
    client.describe_image_chunks(chunks)
    report("extracting")
    extracted = client.extract_facts(chunks)
    report("embedding")
    embeddings = client.embed([chunk["content"] for chunk in chunks])
    if embeddings is None or len(embeddings) != len(chunks):
        raise RuntimeError(
            "Embedding model request failed or returned an incomplete vector set",
        )
    for chunk in chunks:
        chunk.pop("_image_data", None)
    report("storing")
    store.replace_document_content(document, chunks, extracted, embeddings)
    # Record which parser backend was used for this document.
    store.update_document_backend(document["id"], backend)
    # The evidence store is authoritative. Graph indexing is best-effort so a
    # temporary LightRAG/model outage never loses an uploaded document.
    report("graph_indexing")
    asyncio.run(
        LightRAGBridge(settings).insert(
            space_id=document["space_id"],
            document_id=document["id"],
            source_name=document["filename"],
            chunks=chunks,
            # Resolved here rather than passed down from the request: the worker
            # runs minutes later, in another process, and the mapping is the only
            # durable record of which path the upload was for.
            workspace=store.get_document_workspace(document["id"]),
        )
    )
