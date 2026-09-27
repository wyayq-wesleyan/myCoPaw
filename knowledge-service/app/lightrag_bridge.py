from __future__ import annotations

import logging
import re
from hashlib import md5
from typing import Any

import httpx

from .config import Settings

logger = logging.getLogger(__name__)

# 空间作用域的来源标识里，字段之间的分隔符。
#
# 为什么要把空间编进「文件名」，而不是用路径式前缀：
# LightRAG 会对 file_source 调用 ``normalize_file_path``，其内部
# ``canonicalize_parser_hinted_basename`` 只取 ``Path(file_path).name`` ——
# 任何目录形式的前缀都会被剥掉。实测：
#   "knowledge://space-a/doc-1/报告.pdf"  ->  "报告.pdf"
#   "space_a__doc-1__报告.pdf"            ->  "space_a__doc-1__报告.pdf"
# 因此路径式命名空间（原先的 ``knowledge://{space}/...``）在存储里根本不存在，
# 按它过滤永远匹配不到任何行 —— 图谱证据一直没真正参与检索。
#
# 编进文件名还顺带修掉一个数据完整性问题：LightRAG 的 doc_id 是
# ``"doc-" + md5(file_source)``，而 file_source 被规范化成 basename 后，
# **不同账号上传同名文件会算出同一个 doc_id**，图谱里互相覆盖。把空间和文档
# id 编进名字后，doc_id 对每个 (空间, 文档) 唯一。
#
# 这里用 "__" 而不是 "/"：basename 里不能有路径分隔符，否则又被吃掉。
SPACE_SEP = "__"


def _space_slug(space_id: str) -> str:
    """空间标识的稳定 slug，保证不同空间不会折叠成同一个。

    朴素做法（把非法字符统统换成 ``_``）是有损的：``a-b`` 与 ``a_b`` 会变成
    同一个 slug，两个空间就此串数据 —— 这正是 LightRAG 自己处理
    ``LIGHTRAG-WORKSPACE`` 头时的隐患。所以这里在可读部分之外，额外拼一段
    原始 id 的短哈希来兜底：可读性保留给排查，唯一性由哈希保证（不需要
    依赖「当前 space_id 恰好是 UUID 所以映射唯一」这种巧合）。

    空值退化为 ``space``，避免产出空前缀把过滤退化成「匹配一切」。
    """
    raw = str(space_id or "")
    readable = re.sub(r"[^0-9A-Za-z]+", "_", raw).strip("_") or "space"
    # 短哈希只用于消歧；取 12 位十六进制，碰撞概率可忽略。
    digest = md5(raw.encode("utf-8")).hexdigest()[:12]
    return f"{readable}_{digest}"


def _scoped_prefix(space_id: str) -> str:
    """某个空间内所有来源标识的公共前缀（用于过滤）。"""
    return f"{_space_slug(space_id)}{SPACE_SEP}"


def _source_ref(space_id: str, document_id: str, source_name: str) -> str:
    """构造注入 LightRAG 的来源标识，空间与文档 id 编进文件名。"""
    return f"{_scoped_prefix(space_id)}{document_id}{SPACE_SEP}{source_name}"


def _lightrag_doc_id(source_ref: str) -> str:
    """复现 LightRAG 写入时算出的 doc_id，供删除时定位。

    LightRAG 用规范化后的 file_source 作为哈希输入：
    ``doc_id = "doc-" + md5(canonicalize_parser_hinted_basename(file_source))``。
    这里优先复用 LightRAG 自己的规范化函数，保证与写入侧一致；取不到时退化为
    原样哈希（对不含 ``.[hint]`` 后缀的名字结果相同）。
    """
    try:
        from lightrag.parser.routing import (  # type: ignore[import-not-found]
            canonicalize_parser_hinted_basename,
        )

        canonical = canonicalize_parser_hinted_basename(source_ref)
    except Exception:  # noqa: BLE001 - 退化为原样，不影响常规文件名
        logger.debug(
            "lightrag: canonicalize helper unavailable; hashing the raw source ref",
            exc_info=True,
        )
        canonical = source_ref
    return "doc-" + md5(canonical.encode("utf-8")).hexdigest()


class LightRAGBridge:
    """Bridge to a separately deployed LightRAG server.

    Enhances retrieval with LightRAG's graph-aware query API.
    Local PostgreSQL remains the authoritative metadata/evidence store.
    LightRAG provides graph-based semantic search as a complementary signal.

    **隔离方式**：LightRAG 的 ``/query`` 系列接口不接受按请求的 workspace
    （``query_routes`` 里 workspace 出现 0 次），服务端的 ``rag.workspace`` 是
    进程级、启动后不可切换；而按 workspace 切分在文件型存储下会去读一个空的
    工作目录，导致检索全空。所以这里不发送 ``LIGHTRAG-WORKSPACE`` 头，改用
    「写入时把空间编进来源标识 + 读取时按该前缀过滤」。这是过滤式隔离，不是
    存储层强制隔离 —— 因此前缀必须精确、且能真正匹配到存储值（见 ``SPACE_SEP``
    的说明）。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.lightrag_url)

    def _headers(self) -> dict[str, str]:
        return (
            {"X-API-Key": self.settings.lightrag_api_key}
            if self.settings.lightrag_api_key
            else {}
        )

    async def query(self, query: str) -> dict[str, Any] | None:
        """Query LightRAG for graph-aware answer (full response)."""
        if not self.enabled:
            return None
        payload = {
            "query": query,
            "mode": self.settings.lightrag_query_mode,
            "include_references": True,
            "include_chunk_content": True,
        }
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                response = await client.post(
                    f"{self.settings.lightrag_url}/query",
                    headers=self._headers(),
                    json=payload,
                )
                response.raise_for_status()
                return response.json()
        except Exception:
            return None

    async def search(
        self,
        query: str,
        space_id: str,
        top_k: int = 20,
    ) -> list[dict[str, Any]]:
        """Query LightRAG retrieval data (no LLM generation) for fusion.

        The graph is shared, so results are filtered down to the requesting
        space before they are handed back (see the class docstring).
        """
        if not self.enabled:
            return []
        payload = {
            "query": query,
            "mode": self.settings.lightrag_query_mode,
        }
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    f"{self.settings.lightrag_url}/query/data",
                    headers=self._headers(),
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                return self._normalize_lightrag_result(data, space_id)
        except Exception:
            logger.warning("lightrag: graph search failed", exc_info=True)
            return []

    def _normalize_lightrag_result(
        self,
        payload: dict[str, Any],
        space_id: str,
    ) -> list[dict[str, Any]]:
        """Convert LightRAG /query/data response to standard evidence format.

        Only chunks whose stored ``file_path`` carries the requesting space's
        prefix are accepted. Because LightRAG strips any directory component
        from that value, the prefix lives in the *filename* (see
        :func:`_source_ref`); matching on a path-style prefix silently dropped
        every row and left the graph signal unused.
        """
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return []
        scope_prefix = _scoped_prefix(space_id)
        evidence: list[dict[str, Any]] = []
        for index, chunk in enumerate(data.get("chunks") or []):
            content = chunk.get("content") or chunk.get("text") or ""
            if not content:
                continue
            file_path = str(chunk.get("file_path") or "")
            if not file_path.startswith(scope_prefix):
                # Another space's document (the graph is shared) — drop it.
                continue
            evidence.append(
                {
                    "id": f"lightrag-{chunk.get('chunk_id') or index}",
                    "document_id": None,
                    "page": None,
                    "location": file_path or "LightRAG 图谱",
                    "content": content,
                    "filename": file_path,
                    "source": "lightrag",
                    "score": 0.5,
                },
            )
        return evidence

    async def insert(
        self,
        *,
        space_id: str,
        document_id: str,
        source_name: str,
        chunks: list[dict[str, Any]],
    ) -> bool:
        """Index normalized content in LightRAG after local evidence is durable."""
        if not self.enabled or not chunks:
            return False
        text = "\n\n".join(
            f"[{chunk.get('location') or '正文'} | 第 {chunk.get('page') or '?'} 页]\n{chunk['content']}"
            for chunk in chunks
        )
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    f"{self.settings.lightrag_url}/documents/text",
                    headers=self._headers(),
                    json={
                        "text": text,
                        "file_source": _source_ref(
                            space_id,
                            document_id,
                            source_name,
                        ),
                    },
                )
                # 409 = 内容已在图谱中，视为索引成功
                if response.status_code == 409:
                    return True
                response.raise_for_status()
                return True
        except Exception:
            logger.warning(
                "lightrag: indexing failed for space=%s doc=%s",
                space_id,
                document_id,
                exc_info=True,
            )
            return False

    async def reindex_space(self, store, space_id: str) -> dict[str, int]:
        """Reindex all completed documents in a space to LightRAG."""
        if not self.enabled:
            return {"indexed": 0, "failed": 0}

        docs = store.list_documents_internal(space_id)
        indexed = 0
        failed = 0
        for doc in docs:
            if doc.get("status") != "completed":
                continue
            chunks = store.get_document_evidence(doc["id"])
            if chunks and await self.insert(
                space_id=space_id,
                document_id=doc["id"],
                source_name=doc["filename"],
                chunks=chunks,
            ):
                indexed += 1
            else:
                failed += 1
        return {"indexed": indexed, "failed": failed}

    async def delete_document(
        self,
        space_id: str,
        document_id: str,
        source_name: str,
    ) -> bool:
        """Delete one document and its graph data from LightRAG.

        The LightRAG document id is derived from the (space-scoped) source ref,
        so it can only be computed when the space and original filename are
        known — hence the explicit arguments rather than a bare document id.
        """
        if not self.enabled:
            return False
        doc_id = _lightrag_doc_id(
            _source_ref(space_id, document_id, source_name),
        )
        # 文档被真正删除：连带清掉它的抽取缓存，回收空间。
        return await self._delete_doc_id(doc_id, delete_llm_cache=True)

    async def delete_legacy_document(self, source_name: str) -> bool:
        """Delete the pre-upgrade graph record for one document.

        Records written before the space-scoped naming used ``file_source =
        filename``, so their document id is ``md5(basename)``. Those rows are
        invisible to the current space filter (their ``file_path`` carries no
        space) and must be removed before re-indexing: LightRAG dedupes on
        ``content_hash``, so a re-insert without deleting first would be
        rejected as a duplicate and leave the unscoped row in place forever.

        Keeps the LLM cache on purpose: the caller is about to re-index the same
        content, and a cache hit is what makes the migration cheap (it is a
        database rewrite, not a fresh extraction).
        """
        if not self.enabled:
            return False
        return await self._delete_doc_id(
            _lightrag_doc_id(source_name),
            delete_llm_cache=False,
        )

    async def _delete_doc_id(
        self,
        doc_id: str,
        *,
        delete_llm_cache: bool,
    ) -> bool:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.delete(
                    f"{self.settings.lightrag_url}/documents/delete_document",
                    headers=self._headers(),
                    json={
                        "doc_ids": [doc_id],
                        "delete_llm_cache": delete_llm_cache,
                    },
                )
                if response.status_code == 409:
                    return True
                response.raise_for_status()
                return True
        except Exception:
            logger.warning(
                "lightrag: graph delete failed for doc_id=%s",
                doc_id,
                exc_info=True,
            )
            return False

    async def fetch_indexed_doc_ids(self) -> set[str] | None:
        """列出 LightRAG 当前已索引的 doc id；列举失败时返回 ``None``。

        用于判断「哪些文档还没迁移」—— 已迁移的文档其新 doc_id 会出现在这里。
        返回 ``None`` 而不是空集合是刻意的：**列举失败与「一个都没有」必须区分**。
        把失败当成「全都没有」会导致每次启动都全量重建，虽有缓存兜底但会拖慢
        启动；所以失败时调用方应放弃本轮迁移，等下次启动再试。
        """
        if not self.enabled:
            return None
        ids: set[str] = set()
        page = 1
        # 页大小上限 200；给一个页数上限，避免异常时无限翻页。
        page_size = 200
        max_pages = 200
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                while page <= max_pages:
                    response = await client.post(
                        f"{self.settings.lightrag_url}/documents/paginated",
                        headers=self._headers(),
                        json={
                            "page": page,
                            "page_size": page_size,
                            "sort_field": "id",
                            "sort_direction": "asc",
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                    documents = payload.get("documents") or []
                    for doc in documents:
                        doc_id = doc.get("id")
                        if doc_id:
                            ids.add(str(doc_id))
                    if len(documents) < page_size:
                        break
                    page += 1
                else:
                    logger.warning(
                        "lightrag: document listing hit the page cap (%d); "
                        "treating the result as incomplete",
                        max_pages,
                    )
                    return None
        except Exception:
            logger.warning(
                "lightrag: could not list indexed documents; skipping this "
                "migration pass (will retry on a later start)",
                exc_info=True,
            )
            return None
        return ids

    def _expected_doc_ids(self, store, space_id: str) -> dict[str, tuple[str, str]]:
        """某空间里「按新命名应当存在」的 doc_id → (document_id, filename)。

        只包含已完成文档 —— 处理中/失败的还没有分片可索引。
        """
        expected: dict[str, tuple[str, str]] = {}
        for doc in store.list_documents_internal(space_id):
            if doc.get("status") != "completed":
                continue
            filename = doc.get("filename") or ""
            if not filename:
                continue
            document_id = doc["id"]
            doc_id = _lightrag_doc_id(_source_ref(space_id, document_id, filename))
            expected[doc_id] = (document_id, filename)
        return expected

    async def plan_migration(self, store) -> list[dict[str, Any]] | None:
        """判断哪些文档需要迁移。返回 ``None`` 表示本轮无法判断。

        判定依据是**期望的 doc_id 是否已在 LightRAG 中**，而不是依赖任何本地
        记账表：这样判断天然幂等、可自愈（从未成功索引过的文档也会被补上），
        而且不需要新增状态表、不会出现「记账说迁移过、实际没有」的漂移。

        代价是每个空间一次完整文档列举 —— 几十到几百条，启动时一次调用可接受。
        """
        indexed = await self.fetch_indexed_doc_ids()
        if indexed is None:
            return None
        pending: list[dict[str, Any]] = []
        for space in store.list_all_spaces_internal():
            space_id = space["id"]
            for doc_id, (document_id, filename) in self._expected_doc_ids(
                store, space_id,
            ).items():
                if doc_id in indexed:
                    continue
                pending.append(
                    {
                        "space_id": space_id,
                        "document_id": document_id,
                        "filename": filename,
                    },
                )
        return pending

    async def migrate_legacy_documents(
        self,
        store,
        space_id: str,
        *,
        pending: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """把一个空间的历史图谱记录迁移到空间作用域命名。

        升级前的来源标识是裸 ``filename``，LightRAG 存储下来后不含任何空间信息，
        因此当前按空间前缀过滤时全部落选 —— 图谱证据等于没接。迁移做两件事：

        1. 删掉旧 doc_id（``md5(filename)``）的图谱记录；
        2. 用新的空间作用域来源标识重新索引。

        **为什么必须先删。** LightRAG 按 ``content_hash`` 去重（见
        ``pipeline.py`` 的 ``content_hash_to_doc_id``）：直接重新索引会被判为
        重复、旧记录原样留下，迁移等于没做。

        **代价可控。** 实体抽取的 prompt 里不含 ``file_path``（已验证无
        ``{file_path}`` 占位符，file 路径只用于生成引用），所以重建时
        **LLM 响应缓存会命中**，不产生新的抽取调用 —— 迁移只是数据库写入。

        ``pending`` 可由 :meth:`plan_migration` 预先算好并跨空间复用（避免每个
        空间重复列举一次 LightRAG）；不传则在本空间内自行判定。

        逐文档隔离失败：单个文档出错不影响其余，结果里逐条报告，便于断点重跑。
        """
        if not self.enabled:
            return {"enabled": False, "migrated": 0, "failed": 0, "details": []}

        if pending is None:
            indexed = await self.fetch_indexed_doc_ids()
            if indexed is None:
                return {
                    "enabled": True,
                    "space_id": space_id,
                    "migrated": 0,
                    "failed": 0,
                    "skipped": 0,
                    "details": [],
                    "reason": "could not list indexed documents",
                }
            targets = [
                {
                    "document_id": document_id,
                    "filename": filename,
                }
                for doc_id, (document_id, filename) in self._expected_doc_ids(
                    store, space_id,
                ).items()
                if doc_id not in indexed
            ]
        else:
            targets = [item for item in pending if item["space_id"] == space_id]

        migrated = 0
        failed = 0
        details: list[dict[str, str]] = []
        for item in targets:
            document_id = item["document_id"]
            source_name = item["filename"]
            try:
                # 先删旧的无空间归属记录，再按新命名重建。
                await self.delete_legacy_document(source_name)
                chunks = store.get_document_evidence(document_id)
                ok = await self.insert(
                    space_id=space_id,
                    document_id=document_id,
                    source_name=source_name,
                    chunks=chunks,
                )
            except Exception as exc:  # noqa: BLE001 - 逐条隔离
                logger.warning(
                    "lightrag: migration failed for doc=%s",
                    document_id,
                    exc_info=True,
                )
                ok = False
                details.append(
                    {
                        "document_id": document_id,
                        "result": f"error: {type(exc).__name__}",
                    },
                )
            if ok:
                migrated += 1
                details.append({"document_id": document_id, "result": "migrated"})
            else:
                failed += 1
                if not any(d["document_id"] == document_id for d in details):
                    details.append(
                        {"document_id": document_id, "result": "failed"},
                    )
        return {
            "enabled": True,
            "space_id": space_id,
            "migrated": migrated,
            "failed": failed,
            "skipped": 0,
            "details": details,
        }

    async def migrate_all_if_needed(self, store) -> dict[str, Any]:
        """启动时的入口：判断是否需要迁移，需要就自动执行。

        - LightRAG 未配置 → 直接跳过（无图谱可迁）。
        - 无法列举已索引文档（网络/服务异常）→ 放弃本轮，下次启动重试，
          **不**误判为「全部需要迁移」。
        - 无需迁移 → 只记一行日志，不做任何写入。
        - 需要迁移 → 逐空间执行，单空间失败不影响其余。

        本方法不抛异常：它跑在启动路径上，任何失败都只能降级为日志。
        """
        if not self.enabled:
            return {"enabled": False, "needed": False}

        try:
            pending = await self.plan_migration(store)
        except Exception:  # noqa: BLE001 - 启动路径不允许抛出
            logger.warning("lightrag: migration planning failed", exc_info=True)
            return {"enabled": True, "needed": None, "error": "planning failed"}

        if pending is None:
            return {"enabled": True, "needed": None, "error": "listing failed"}
        if not pending:
            logger.info("lightrag: graph naming is up to date; nothing to migrate")
            return {"enabled": True, "needed": False, "migrated": 0, "failed": 0}

        # 按空间归并，避免每个空间重复列举一次 LightRAG。
        by_space: dict[str, list[dict[str, Any]]] = {}
        for item in pending:
            by_space.setdefault(item["space_id"], []).append(item)

        logger.warning(
            "lightrag: migrating %d document(s) in %d space(s) to the "
            "space-scoped naming — this one-time upgrade makes the previously "
            "unreachable graph evidence visible to retrieval",
            len(pending),
            len(by_space),
        )

        total_migrated = 0
        total_failed = 0
        per_space: list[dict[str, Any]] = []
        for space_id, items in by_space.items():
            try:
                result = await self.migrate_legacy_documents(
                    store, space_id, pending=items,
                )
            except Exception:  # noqa: BLE001 - 逐空间隔离
                logger.warning(
                    "lightrag: migration failed for space %s",
                    space_id,
                    exc_info=True,
                )
                result = {"migrated": 0, "failed": len(items)}
            total_migrated += int(result.get("migrated", 0))
            total_failed += int(result.get("failed", 0))
            per_space.append({"space_id": space_id, **result})

        logger.warning(
            "lightrag: migration finished — %d migrated, %d failed",
            total_migrated,
            total_failed,
        )
        return {
            "enabled": True,
            "needed": True,
            "migrated": total_migrated,
            "failed": total_failed,
            "spaces": per_space,
        }

    async def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.settings.lightrag_url}/health")
                return {
                    "enabled": True,
                    "reachable": response.is_success,
                    "status_code": response.status_code,
                }
        except Exception as exc:
            return {"enabled": True, "reachable": False, "error": str(exc)}
