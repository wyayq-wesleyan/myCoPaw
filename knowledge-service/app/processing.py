from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .config import Settings
from .lightrag_bridge import LightRAGBridge
from .parsers import parse_document
from .providers import ModelClient
from .store import Store


def process_document(store: Store, settings: Settings, job: dict[str, Any]) -> None:
    document = store.get_document_by_id_for_worker(job["document_id"])
    if not document:
        raise RuntimeError(f"Document not found: {job['document_id']}")
    backend, chunks = parse_document(Path(document["path"]), settings.parser_backend, settings.mineru_command)
    for chunk in chunks:
        chunk["space_id"] = document["space_id"]
    client = ModelClient(settings)
    extracted = client.extract_facts(chunks)
    embeddings = client.embed([chunk["content"] for chunk in chunks])
    store.replace_document_content(document, chunks, extracted, embeddings)
    # The evidence store is authoritative. Graph indexing is best-effort so a
    # temporary LightRAG/model outage never loses an uploaded document.
    asyncio.run(LightRAGBridge(settings).insert(document["filename"], chunks))
