"""Account-scoped retrieval tool backed by the standalone knowledge service."""

from __future__ import annotations

import os
from typing import Any

import httpx
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from ...app.agent_context import get_current_agent_id, get_current_user_id
from ...runtime.tool_registry import tool_descriptor


def _service_url() -> str:
    return os.environ.get("QWENPAW_KNOWLEDGE_URL", "").strip().rstrip("/")


def _headers(username: str) -> dict[str, str]:
    headers = {"X-User-Id": username, "X-Is-Admin": "true" if username == "admin" else "false"}
    key = os.environ.get("QWENPAW_KNOWLEDGE_API_KEY", "").strip()
    if key:
        headers["X-Knowledge-Key"] = key
    return headers


def _resolve_account_username() -> str:
    """Resolve the owning account for both interactive and scheduled runs."""
    username = get_current_user_id()
    if username and username != "default":
        return username
    try:
        from ...config import load_config

        profile = load_config().agents.profiles.get(get_current_agent_id())
        owner = getattr(profile, "owner_username", None) if profile else None
        if isinstance(owner, str) and owner.strip():
            return owner.strip()
    except Exception:
        pass
    return "admin"


def _format_search_result(payload: dict[str, Any]) -> str:
    """格式化检索结果，只返回文档列表和摘要，不返回完整内容"""
    documents = payload.get("documents") or []
    evidence = payload.get("evidence") or []
    total_count = payload.get("total_count", 0)
    document_count = payload.get("document_count", 0)

    if not documents:
        return "未找到相关文档。"

    lines = [f"找到 {document_count} 个相关文档，共 {total_count} 个相关片段：\n"]

    for i, doc in enumerate(documents[:5], 1):
        doc_id = doc.get("document_id", "")
        filename = doc.get("filename", "未知文件")
        snippet_count = doc.get("snippet_count", 0)
        pages = doc.get("pages", [])
        pages_str = f"，涉及页码：{', '.join(str(p) for p in sorted(pages)[:5])}" if pages else ""
        lines.append(f"[{i}] {filename}（{snippet_count} 个相关片段{pages_str}）")
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

    Use this whenever the user asks about uploaded reference files, historical
    materials, people, events, relationships, or facts that may be stored in
    a knowledge space. Do not claim that no relevant materials exist unless
    the returned evidence is empty. ``space_name`` is optional; omit it to
    search all spaces available to the current account.

    Returns a list of relevant documents and snippet counts. Use
    get_document_detail to retrieve the actual content of specific documents.
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
                matched = [
                    item for item in spaces
                    if not space_name or space_name.strip().lower() in str(item.get("name") or "").lower()
                ]
                if not matched:
                    text = "当前账户下没有可检索的知识空间。"
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
        except httpx.HTTPError:
            text = "知识服务暂时不可用，请稍后重试。"
        except Exception:
            # 不把原始异常带进模型上下文，避免内部地址/路径泄漏到回复
            text = "知识检索发生内部错误，请稍后重试。"
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
        except httpx.HTTPError:
            text = "知识服务暂时不可用，请稍后重试。"
        except Exception:
            text = "获取文档详情发生内部错误，请稍后重试。"

    return ToolChunk(
        is_last=True,
        state=ToolResultState.SUCCESS,
        content=[TextBlock(type="text", text=text)],
    )
