"""Account-scoped retrieval tool backed by the standalone knowledge service."""

from __future__ import annotations

import asyncio
import mimetypes
import os
import time
from typing import Any

import httpx
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from ...runtime.tool_registry import tool_descriptor
from .user_files import resolve_user_file, user_facing_name

# Mirrors the suffix allowlist enforced by the knowledge service.
_KNOWLEDGE_EXTS = frozenset(
    {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".csv", ".txt", ".md", ".json"},
)
# Parsing runs in the knowledge worker; wait a bounded time so small files
# report completion and large ones hand back a document id instead of blocking.
_UPLOAD_POLL_TIMEOUT = 45.0
_UPLOAD_POLL_INTERVAL = 2.0
_UPLOAD_TERMINAL_STATES = frozenset({"completed", "failed"})


def _service_url() -> str:
    return os.environ.get("QWENPAW_KNOWLEDGE_URL", "").strip().rstrip("/")


def _headers(username: str) -> dict[str, str]:
    headers = {"X-User-Id": username, "X-Is-Admin": "true" if username == "admin" else "false"}
    key = os.environ.get("QWENPAW_KNOWLEDGE_API_KEY", "").strip()
    if key:
        headers["X-Knowledge-Key"] = key
    return headers


def _resolve_account_username() -> str:
    """Resolve the owning account for both interactive and scheduled runs.

    Delegates to the shared resolver so the account rule — including
    refusing rather than defaulting to ``admin`` when the identity cannot be
    determined under authentication — lives in exactly one place.
    """
    from .user_files import resolve_account_username

    return resolve_account_username()


def _format_search_result(payload: dict[str, Any]) -> str:
    """格式化检索结果，包含来源和置信度信息"""
    documents = payload.get("documents") or []
    evidence = payload.get("evidence") or []
    total_count = payload.get("total_count", 0)
    document_count = payload.get("document_count", 0)

    if not documents:
        return "未找到相关文档。"

    SOURCE_LABEL = {
        "fulltext": "全文检索",
        "filename": "文件名匹配",
        "literal": "关键词匹配",
        "semantic": "语义相似",
        "entity": "实体匹配",
        "lightrag": "知识图谱",
    }
    CONFIDENCE_LABEL = {
        "high": "高置信",
        "medium": "中置信",
        "low": "低置信",
    }

    lines = [f"找到 {document_count} 个相关文档，共 {total_count} 个相关片段：\n"]

    for i, doc in enumerate(documents[:5], 1):
        doc_id = doc.get("document_id", "")
        filename = doc.get("filename", "未知文件")
        snippet_count = doc.get("snippet_count", 0)
        pages = doc.get("pages", [])
        sources = doc.get("sources", [])
        pages_str = f"，涉及页码：{', '.join(str(p) for p in sorted(pages)[:5])}" if pages else ""
        sources_str = "，来源：" + "、".join(SOURCE_LABEL.get(s, s) for s in sources) if sources else ""
        lines.append(f"[{i}] {filename}（{snippet_count} 个相关片段{pages_str}{sources_str}）")
        lines.append(f"    文档ID: {doc_id}")

    if document_count > 5:
        lines.append(f"\n... 还有 {document_count - 5} 个文档未显示")

    lines.append("\n提示：使用 get_document_detail 工具查看特定文档的详细内容。")

    return "\n".join(lines)


@tool_descriptor(
    name="query_knowledge",
    async_execution=True,
    tool_type="internal",
    policy_name="QueryKnowledge",
    default_policy="allow",
    policy_reason="Read-only retrieval within the current account's knowledge spaces",
    ui_description="Search the current account's local knowledge spaces",
    ui_icon="📚",
)
async def query_knowledge(query: str, space_name: str = "") -> ToolChunk:
    """Search local knowledge before answering questions grounded in uploaded materials.

    检索来源说明：
    - 全文检索(fulltext)：直接匹配原文，可信度最高
    - 文件名匹配(filename)：查询词出现在文件名里（版本号、编号、年份等只写在文件名的信息）
    - 关键词匹配(literal)：CJK 二元组部分匹配，可信度高
    - 语义相似(semantic)：embedding 向量相似，可能不完全精确
    - 实体匹配(entity)：人名/机构名等实体匹配
    - 知识图谱(lightrag)：图谱关系推断，需核实原文

    回答时优先使用"全文检索"、"文件名匹配"和"关键词匹配"来源的内容，对"知识图谱"
    来源的推断性内容需谨慎使用，尽量引用原文佐证。
    """
    url = _service_url()
    username = _resolve_account_username()
    query = query.strip()
    if not url:
        text = "知识服务尚未配置，无法检索本地知识空间。"
    elif not query:
        text = "检索关键词不能为空。"
    else:
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                spaces_response = await client.get(f"{url}/v1/spaces", headers=_headers(username))
                spaces_response.raise_for_status()
                spaces = spaces_response.json()
                # Path scoping: the account owns these spaces, but this agent may
                # only read the ones whose path it was granted (an unbound space
                # is public and lives in the account root). Filtering here — rather
                # than letting the model pick — is the enforcement point; a space
                # the agent was not granted must not even be searchable.
                from .path_scope import visible_space_ids

                allowed_ids = set(
                    visible_space_ids(
                        username,
                        [str(item.get("id") or "") for item in spaces],
                    ),
                )
                matched = [
                    item for item in spaces
                    if str(item.get("id") or "") in allowed_ids
                    and (
                        not space_name
                        or space_name.strip().lower()
                        in str(item.get("name") or "").lower()
                    )
                ]
                if not matched:
                    text = (
                        "当前智能体没有可检索的知识空间。"
                        "请在知识空间中为该智能体挂载对应的路径。"
                        if not allowed_ids
                        else "当前账户下没有匹配的知识空间。"
                    )
                else:
                    results: list[tuple[str, dict[str, Any]]] = []
                    for space in matched:
                        response = await client.post(
                            f"{url}/v1/search",
                            headers=_headers(username),
                            json={"space_id": space["id"], "query": query, "top_k": 12},
                        )
                        response.raise_for_status()
                        results.append((str(space.get("name") or "知识空间"), response.json()))
                    useful = [(name, payload) for name, payload in results if payload.get("evidence")]
                    target = useful or results
                    text = "\n\n".join(f"知识空间：{name}\n{_format_search_result(payload)}" for name, payload in target)
        except httpx.HTTPStatusError as exc:
            text = f"知识检索失败：服务返回 {exc.response.status_code}。"
        except httpx.HTTPError as exc:
            text = f"知识服务暂时不可用：{exc}"
        except Exception as exc:  # Keep tool failures readable to the model.
            text = f"知识检索发生错误：{exc}"
    return ToolChunk(
        is_last=True,
        state=ToolResultState.SUCCESS,
        content=[TextBlock(type="text", text=text)],
    )


def _format_document_detail(payload: dict[str, Any]) -> str:
    """格式化文档详情"""
    filename = payload.get("filename", "未知文件")
    chunks = payload.get("chunks") or []
    total_count = payload.get("total_count", 0)

    if not chunks:
        return f"文档 {filename} 没有可读取的内容片段。"

    lines = [f"文档：{filename}（共 {total_count} 个片段）\n"]

    remaining = 12_000
    shown = 0
    for i, chunk in enumerate(chunks[:20], 1):
        page = chunk.get("page")
        location = chunk.get("location", "")
        content = chunk.get("content", "")
        if remaining <= 0:
            break
        position = f"第 {page} 页" if page else ""
        if location:
            position = f"{position} - {location}" if position else location
        lines.append(f"[{i}] {position}")
        limit = min(1_200, remaining)
        lines.append(f"{content[:limit]}{'...' if len(content) > limit else ''}")
        lines.append("")
        remaining -= min(len(content), limit)
        shown += 1

    if total_count > shown:
        lines.append(f"... 还有 {total_count - shown} 个片段未显示，可按 PDF 页码筛选；DOCX/TXT 等无可靠页码文档请按片段位置读取。")

    return "\n".join(lines)


@tool_descriptor(
    name="get_document_detail",
    async_execution=True,
    tool_type="internal",
    policy_name="GetDocumentDetail",
    default_policy="allow",
    policy_reason="Read-only retrieval within the current account's knowledge spaces",
    ui_description="Get detailed content of a specific document from knowledge space",
    ui_icon="📄",
)
async def get_document_detail(document_id: str, page_start: int = 0, page_end: int = 0, max_chunks: int = 20) -> ToolChunk:
    """Get detailed content of a specific document from knowledge space.

    Use this tool after query_knowledge returns document lists to retrieve
    the actual content of specific documents. You can filter by page range.

    Args:
        document_id: The document ID returned by query_knowledge
        page_start: Start PDF page number (0 means no filter)
        page_end: End PDF page number (0 means no filter)
        max_chunks: Maximum number of chunks to return (default 20)
    """
    url = _service_url()
    username = _resolve_account_username()
    document_id = document_id.strip()

    if not url:
        text = "知识服务尚未配置，无法检索本地知识空间。"
    elif not document_id:
        text = "文档ID不能为空。"
    else:
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                payload: dict[str, Any] = {
                    "document_id": document_id,
                    "max_chunks": max_chunks,
                }
                if page_start > 0:
                    payload["page_start"] = page_start
                if page_end > 0:
                    payload["page_end"] = page_end

                response = await client.post(
                    f"{url}/v1/documents/chunks",
                    headers=_headers(username),
                    json=payload,
                )
                response.raise_for_status()
                text = _format_document_detail(response.json())
        except httpx.HTTPStatusError as exc:
            text = f"获取文档详情失败：服务返回 {exc.response.status_code}。"
        except httpx.HTTPError as exc:
            text = f"知识服务暂时不可用：{exc}"
        except Exception as exc:
            text = f"获取文档详情发生错误：{exc}"

    return ToolChunk(
        is_last=True,
        state=ToolResultState.SUCCESS,
        content=[TextBlock(type="text", text=text)],
    )


def _space_chunk(text: str) -> ToolChunk:
    return ToolChunk(
        is_last=True,
        state=ToolResultState.SUCCESS,
        content=[TextBlock(type="text", text=text)],
    )


async def _known_spaces(url: str, username: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(f"{url}/v1/spaces", headers=_headers(username))
        response.raise_for_status()
        return response.json() or []


def _find_space(spaces: list[dict[str, Any]], space_name: str) -> dict[str, Any] | None:
    wanted = space_name.strip().lower()
    for space in spaces:
        if str(space.get("name") or "").strip().lower() == wanted:
            return space
    for space in spaces:  # fall back to a substring match
        if wanted and wanted in str(space.get("name") or "").strip().lower():
            return space
    return None


@tool_descriptor(
    name="create_knowledge_space",
    async_execution=True,
    tool_type="network",
    policy_name="CreateKnowledgeSpace",
    default_policy="ask",
    policy_reason="Creates a knowledge space owned by the current account",
    ui_description="为当前用户新建知识空间",
    ui_icon="📁",
)
async def create_knowledge_space(name: str) -> ToolChunk:
    """Create a knowledge space for the current account.

    Args:
        name: 知识空间名称，用能说明内容的短名称，例如"采购合同模板"。
    """
    url = _service_url()
    username = _resolve_account_username()
    name = (name or "").strip()
    if not url:
        return _space_chunk("知识服务尚未配置，无法新建知识空间。")
    if not name:
        return _space_chunk("请提供知识空间名称。")

    try:
        existing = _find_space(await _known_spaces(url, username), name)
        if existing:
            return _space_chunk(
                f"已存在知识空间「{existing.get('name')}」（ID: {existing.get('id')}），"
                f"直接使用即可，未重复创建。"
            )
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{url}/v1/spaces",
                headers=_headers(username),
                json={"name": name},
            )
            response.raise_for_status()
            space = response.json()
        return _space_chunk(
            f"已创建知识空间「{space.get('name')}」（ID: {space.get('id')}）。"
            f"接下来可用 upload_knowledge_document 把文件上传进去。"
        )
    except httpx.HTTPStatusError as exc:
        return _space_chunk(f"新建知识空间失败：服务返回 {exc.response.status_code}。")
    except httpx.HTTPError as exc:
        return _space_chunk(f"知识服务暂时不可用：{exc}")
    except Exception as exc:  # Keep tool failures readable to the model.
        return _space_chunk(f"新建知识空间发生错误：{exc}")


def _document_state(payload: dict[str, Any]) -> str:
    status = payload.get("status") or "queued"
    stage = payload.get("processing_stage")
    if status == "failed":
        return f"处理失败：{payload.get('error') or '解析失败'}"
    if status == "completed":
        return "已入库"
    return f"处理中（{stage or status}）"


async def _await_document(url: str, username: str, document_id: str, filename: str) -> str:
    deadline = time.monotonic() + _UPLOAD_POLL_TIMEOUT
    payload: dict[str, Any] = {}
    while True:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.get(
                    f"{url}/v1/documents/{document_id}",
                    headers=_headers(username),
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError:
            payload = {}
        if payload.get("status") in _UPLOAD_TERMINAL_STATES:
            break
        if time.monotonic() >= deadline:
            state = payload.get("status") or "queued"
            return (
                f"已上传 {filename}（文档ID: {document_id}），当前状态：{state}。"
                "解析需要时间，稍后用 query_knowledge 检索即可。"
            )
        await asyncio.sleep(_UPLOAD_POLL_INTERVAL)

    state = _document_state(payload)
    if state.startswith("处理失败"):
        return f"上传了 {filename}（文档ID: {document_id}），但{state}。"
    return f"已上传并入库：{filename}（文档ID: {document_id}）。现在可用 query_knowledge 检索它。"


@tool_descriptor(
    name="upload_knowledge_document",
    async_execution=True,
    tool_type="network",
    target_param="file",
    policy_name="UploadKnowledgeDocument",
    default_policy="ask",
    policy_reason="Uploads a user file into the current account's knowledge space",
    ui_description="把用户上传的文件上传到知识空间",
    ui_icon="📤",
)
async def upload_knowledge_document(file: str, space_name: str = "") -> ToolChunk:
    """Upload a user's file into a knowledge space so it can be searched later.

    Args:
        file: 要上传的文件名或路径，例如"采购合同.pdf"。聊天附件直接传文件名即可。
        space_name: 目标知识空间名称；留空则上传到当前用户的默认知识空间。
    """
    url = _service_url()
    username = _resolve_account_username()
    if not url:
        return _space_chunk("知识服务尚未配置，无法上传到知识空间。")

    path, error = resolve_user_file(file, _KNOWLEDGE_EXTS)
    if path is None:
        return _space_chunk(error)

    space_id = ""
    if space_name.strip():
        try:
            space = _find_space(await _known_spaces(url, username), space_name)
        except httpx.HTTPError as exc:
            return _space_chunk(f"知识服务暂时不可用：{exc}")
        if space is None:
            return _space_chunk(
                f"没有找到名为「{space_name}」的知识空间。"
                f"可以先用 create_knowledge_space 新建，或留空 space_name 上传到默认空间。"
            )
        space_id = str(space.get("id") or "")

    display_name = user_facing_name(path)
    content_type = mimetypes.guess_type(display_name)[0] or "application/octet-stream"
    try:
        with path.open("rb") as handle:
            files = {"file": (display_name, handle, content_type)}
            data = {"space_id": space_id} if space_id else {}
            async with httpx.AsyncClient(timeout=3600) as client:
                response = await client.post(
                    f"{url}/v1/documents/upload",
                    headers=_headers(username),
                    files=files,
                    data=data,
                )
                if response.status_code == 409:
                    # Embedding/vision models are not ready; the service says why.
                    try:
                        detail = str(response.json().get("detail") or "")
                    except Exception:
                        detail = ""
                    return _space_chunk(
                        f"暂时无法上传：{detail or '知识服务尚未就绪。'}",
                    )
                response.raise_for_status()
                created = response.json()
    except httpx.HTTPStatusError as exc:
        return _space_chunk(f"上传失败：服务返回 {exc.response.status_code}。")
    except httpx.HTTPError as exc:
        return _space_chunk(f"知识服务暂时不可用：{exc}")
    except Exception as exc:
        return _space_chunk(f"上传发生错误：{exc}")

    document_id = str(created.get("id") or "")
    filename = str(created.get("filename") or display_name)
    if created.get("duplicate"):
        return _space_chunk(
            f"知识空间里已有相同内容的文件 {filename}（文档ID: {document_id}），未重复上传。"
        )
    if not document_id:
        return _space_chunk(f"上传返回异常，未取得文档ID：{created}")

    return _space_chunk(await _await_document(url, username, document_id, filename))
