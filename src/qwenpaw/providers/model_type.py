"""Model endpoint type inference for local and OpenAI-compatible services."""

from __future__ import annotations

import re
from typing import Literal

ModelType = Literal["chat", "embedding", "reranker"]


def infer_model_type(model_id: str, model_name: str = "") -> ModelType:
    """Infer a conservative default from a model identifier."""
    value = re.sub(r"[^a-z0-9]+", "-", f"{model_id} {model_name}".lower())
    if any(
        marker in value
        for marker in (
            "embedding", "text-embedding", "embed-", "-embed",
            "bge-m3", "bge-large", "e5-", "gte-",
        )
    ):
        return "embedding"
    if any(
        marker in value
        for marker in (
            "rerank", "reranker", "cross-encoder", "bge-reranker",
        )
    ):
        return "reranker"
    return "chat"
