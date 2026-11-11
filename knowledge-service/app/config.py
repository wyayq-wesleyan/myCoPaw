from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import httpx


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _fetch_models_from_provider(base_url: str, api_key: str) -> list[dict]:
    """Fetch available models from an OpenAI-compatible provider."""
    if not base_url:
        return []
    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        response = httpx.get(f"{base_url.rstrip('/')}/models", headers=headers, timeout=10)
        response.raise_for_status()
        return response.json().get("data", [])
    except Exception as e:
        print(f"Warning: Failed to fetch models from {base_url}: {e}")
        return []


def _find_model_by_pattern(models: list[dict], pattern: str) -> str:
    """Find first model whose ID contains the pattern (case-insensitive)."""
    pattern_lower = pattern.lower()
    for model in models:
        if pattern_lower in model.get("id", "").lower():
            return model["id"]
    return ""


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    api_key: str
    worker_interval: float
    max_file_mb: int
    parser_backend: str
    mineru_command: str
    mineru_timeout_seconds: int
    knowledge_offline: bool
    lightrag_url: str
    lightrag_api_key: str
    lightrag_query_mode: str

    # Tenant-isolated graph storage (see docs/知识服务多租户存储设计方案.md).
    # When ``graph_database_url`` is set, the graph runs **in-process** against
    # PostgreSQL with one LightRAG workspace per path, and ``lightrag_url`` is
    # ignored. Leaving it empty keeps the legacy shared-graph + HTTP behaviour, so
    # a deployment can switch over without a code rollback.
    graph_database_url: str
    # Resident LightRAG instances kept alive per process (LRU-evicted above this).
    # This bounds **memory**, not connections: every workspace shares the single
    # process-wide PostgreSQL pool that LightRAG creates, so the connection count
    # is ``graph_pool_max_connections`` per process regardless of this value.
    graph_max_workspaces: int
    # Size of that one shared pool. Total graph connections per process = this.
    graph_pool_max_connections: int
    chat_base_url: str
    chat_api_key: str
    chat_model: str
    embedding_base_url: str
    embedding_api_key: str
    embedding_model: str
    embedding_explicitly_configured: bool
    embedding_dimensions: int | None
    reranker_base_url: str
    reranker_api_key: str
    reranker_model: str
    reranker_enabled: bool

    @classmethod
    def from_env(cls) -> "Settings":
        dimensions = _env("EMBEDDING_DIMENSIONS")

        # Provider configuration
        # 模型服务与容器同机：host 网络下直接用回环地址。（模型服务在别的机器时
        # 由 .env 的 CHAT_BASE_URL / PROVIDER_BASE_URL 指向它的局域网地址。）
        provider_base_url = _env("PROVIDER_BASE_URL", "http://127.0.0.1:9988/v1")
        provider_api_key = _env("PROVIDER_API_KEY", "")

        # Chat model
        chat_base_url = _env("CHAT_BASE_URL") or provider_base_url
        chat_api_key = _env("CHAT_API_KEY") or provider_api_key
        chat_model = _env("CHAT_MODEL")

        # Embedding model
        explicit_embedding_base_url = _env("EMBEDDING_BASE_URL")
        explicit_embedding_model = _env("EMBEDDING_MODEL")
        embedding_base_url = explicit_embedding_base_url or provider_base_url
        embedding_api_key = _env("EMBEDDING_API_KEY") or provider_api_key
        embedding_model = explicit_embedding_model

        # Reranker model
        reranker_base_url = _env("RERANKER_BASE_URL") or provider_base_url
        reranker_api_key = _env("RERANKER_API_KEY") or provider_api_key
        reranker_model = _env("RERANKER_MODEL")
        reranker_enabled_str = _env("RERANKER_ENABLED")

        # Auto-discover models if not configured
        if not chat_model:
            all_models = _fetch_models_from_provider(chat_base_url, chat_api_key)

            if not chat_model:
                # Chat model: use patterns to identify
                for pattern in ["chat", "instruct", "rl", "base", "it-"]:
                    found = _find_model_by_pattern(all_models, pattern)
                    if found:
                        chat_model = found
                        break
                if not chat_model and all_models:
                    # Fallback: use first model that's not embedding/reranker
                    for m in all_models:
                        mid = m.get("id", "").lower()
                        if "embed" not in mid and "rerank" not in mid:
                            chat_model = m["id"]
                            break

            if not reranker_model:
                reranker_model = _find_model_by_pattern(all_models, "rerank")
                if reranker_model and not reranker_enabled_str:
                    reranker_enabled_str = "true"

        return cls(
            data_dir=Path(_env("KNOWLEDGE_DATA_DIR", "/data")).expanduser(),
            # 默认指向宿主机的回环地址：所有容器都用 network_mode: host，
            # PostgreSQL 就监听在宿主机的 15432（见 docker-compose.postgres.yml）。
            # 这里曾写成容器名 knowledge-postgres:5432 —— bridge 网络下才成立，
            # 一旦容器不再共享同一个 compose 网络就会连不上（线上出过一次）。
            database_url=_env(
                "KNOWLEDGE_DATABASE_URL",
                "postgresql://huizhi_knowledge:huizhi_knowledge@127.0.0.1:15432/huizhi_knowledge",
            ),
            api_key=_env("KNOWLEDGE_API_KEY"),
            worker_interval=max(0.2, float(_env("KNOWLEDGE_WORKER_INTERVAL", "1"))),
            # 上限以 .env 为准，10 GiB 只是**未设置时**的默认值。
            #
            # 原先是 `max(10240, int(env))`，即环境变量只能调大不能调小：运维写
            # `KNOWLEDGE_MAX_FILE_MB=100`，实际生效是 10240，配置静默失效。这不只是
            # 「认知不一致」——上传会把整个文件读进内存（parsers.py 的轻量解析器），
            # 10 GB 的文件足以把 worker 打 OOM，而运营方以为自己已经限到 100 MB。
            # 想放大文件就显式写大值（如 10240），不要靠默认值兜底。
            max_file_mb=max(1, int(_env("KNOWLEDGE_MAX_FILE_MB", "10240"))),
            parser_backend=_env("PARSER_BACKEND", "auto").lower(),
            mineru_command=_env("MINERU_COMMAND"),
            mineru_timeout_seconds=max(60, int(_env("MINERU_TIMEOUT_SECONDS", "900"))),
            knowledge_offline=_env("KNOWLEDGE_OFFLINE", "false").lower() in {"1", "true", "yes", "on"},
            lightrag_url=_env("LIGHTRAG_URL").rstrip("/"),
            graph_database_url=_env("KNOWLEDGE_GRAPH_DATABASE_URL"),
            graph_max_workspaces=int(_env("KNOWLEDGE_GRAPH_MAX_WORKSPACES", "32") or "32"),
            graph_pool_max_connections=int(
                _env("KNOWLEDGE_GRAPH_POOL_MAX_CONNECTIONS", "8") or "8"
            ),
            lightrag_api_key=_env("LIGHTRAG_API_KEY"),
            lightrag_query_mode=_env("LIGHTRAG_QUERY_MODE", "mix"),
            chat_base_url=chat_base_url.rstrip("/"),
            chat_api_key=chat_api_key,
            chat_model=chat_model or "llm",
            embedding_base_url=embedding_base_url.rstrip("/"),
            embedding_api_key=embedding_api_key,
            embedding_model=embedding_model,
            embedding_explicitly_configured=bool(
                explicit_embedding_base_url and explicit_embedding_model
            ),
            embedding_dimensions=int(dimensions) if dimensions.isdigit() else None,
            reranker_base_url=reranker_base_url.rstrip("/"),
            reranker_api_key=reranker_api_key,
            reranker_model=reranker_model,
            reranker_enabled=reranker_enabled_str.lower() in {"1", "true", "yes"},
        )

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)
