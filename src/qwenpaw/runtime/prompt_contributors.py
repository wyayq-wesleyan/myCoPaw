# -*- coding: utf-8 -*-
"""Built-in :class:`PromptContributor` implementations.

Each contributor is responsible for one fragment of the system prompt.
``build_default_prompt_manager`` assembles a :class:`PromptManager`
pre-loaded with the built-in contributors, ready for ``build_sync(ctx)``.

Contributors read configuration from ``ctx.extras``:

* ``workspace_dir`` — from ``ctx.workspace_dir``
* ``agent_id``      — from ``ctx.agent_id``
* ``language``      — ``ctx.extras["language"]`` (default ``"zh"``)
* ``heartbeat_enabled`` — ``ctx.extras.get("heartbeat_enabled", False)``
* ``env_context``       — ``ctx.extras.get("env_context")``
* ``agent_config``      — ``ctx.extras.get("agent_config")``
* ``driver_prompt_hints`` — ``ctx.extras.get("driver_prompt_hints", [])``
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .prompt_manager import PromptManager, SyncPromptContributor

if TYPE_CHECKING:
    from .hooks import HookContext

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT_FILES = ("AGENTS.md", "SOUL.md", "PROFILE.md")

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_HEARTBEAT_PATTERN = re.compile(
    r"<!-- heartbeat:start -->.*?<!-- heartbeat:end -->",
    re.DOTALL,
)
_MEMORY_PATTERN = re.compile(
    r"<!-- memory:start -->.*?<!-- memory:end -->",
    re.DOTALL,
)


def _read_prompt_file(workspace_dir: Path, filename: str) -> str | None:
    """Read a markdown file from *workspace_dir* / *filename*.

    If the file starts with a ``---``-delimited YAML frontmatter block,
    that block is stripped and only the body content is returned.
    Returns ``None`` when the file does not exist or is empty after
    stripping.
    """
    try:
        workspace_root = workspace_dir.resolve()
        path = (workspace_root / filename).resolve()
        if path == workspace_root or not path.is_relative_to(workspace_root):
            logger.warning(
                "Prompt file %s resolves outside workspace, skipping",
                filename,
            )
            return None
        if not path.is_file():
            return None

        from ..agents.utils.file_handling import (
            read_text_file_with_encoding_fallback,
        )

        content = read_text_file_with_encoding_fallback(path).strip()
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                content = parts[2].strip()
        return content or None
    except Exception:
        logger.warning("Failed to read %s, skipping", filename)
        return None


def _system_prompt_files(ctx: "HookContext") -> list[str]:
    """Return prompt files configured for the current agent.

    ``None`` means the profile predates the field, so use the historical
    defaults. An empty list is meaningful and disables workspace prompt files.
    """
    extras = getattr(ctx, "extras", {}) or {}
    agent_config = extras.get("agent_config")
    if agent_config is None:
        return list(DEFAULT_SYSTEM_PROMPT_FILES)
    files = getattr(agent_config, "system_prompt_files", None)
    if files is None:
        return list(DEFAULT_SYSTEM_PROMPT_FILES)
    return [str(filename) for filename in files]


def _process_heartbeat_section(content: str, enabled: bool) -> str:
    if "<!-- heartbeat:start -->" not in content:
        return content
    if enabled:
        content = content.replace("<!-- heartbeat:start -->", "")
        content = content.replace("<!-- heartbeat:end -->", "")
        return content.strip()
    return _HEARTBEAT_PATTERN.sub("", content).strip()


def _process_memory_section(
    content: str,
    memory_manager: Any | None,
) -> str:
    if "<!-- memory:start -->" in content:
        content = _MEMORY_PATTERN.sub("", content).strip()
    memory_section = ""
    if memory_manager is not None:
        memory_section = memory_manager.get_memory_prompt()
    if content and memory_section:
        return (content + "\n\n" + memory_section).strip()
    return (content or memory_section).strip()


# ---------------------------------------------------------------------------
# Contributors
# ---------------------------------------------------------------------------


class AgentIdentityContributor(SyncPromptContributor):
    """Prepend agent identity header when ``agent_id`` is set."""

    name = "agent_identity"
    priority = 5

    def contribute_sync(self, ctx: "HookContext") -> str | None:
        agent_id = getattr(ctx, "agent_id", None)
        if not agent_id:
            return None
        return (
            f"# Agent Identity\n\n"
            f"Your agent id is `{agent_id}`. "
            f"This is your unique identifier in the multi-agent system."
        )


class AccountBoundaryContributor(SyncPromptContributor):
    """State that this account's workspace is the boundary, and name it.

    This deployment is multi-user: every account gets its own workspace root,
    and the file/shell tools refuse paths outside it. Without being told, a
    model reasons from what the filesystem appears to contain, so it will
    happily try a path it saw in a log or an earlier example and then report a
    confusing "permission" error instead of staying in its own space. Naming
    the root up front turns a class of failed tool calls into correct ones.

    Path containment still carries a code-level check; this is guidance, not a
    security control, so it is not a substitute for that enforcement.
    """

    name = "account_boundary"
    priority = 6

    def contribute_sync(self, ctx: "HookContext") -> str | None:
        workspace_dir = getattr(ctx, "workspace_dir", None)
        if not workspace_dir:
            return None
        language = (getattr(ctx, "extras", {}) or {}).get("language", "zh")
        root = str(workspace_dir)
        if language == "zh":
            return (
                "# 账号数据边界\n\n"
                f"本系统是多用户系统，你只能访问**当前账号**工作区内的文件：`{root}`。\n\n"
                "1. 所有文件读写、搜索、命令执行都必须使用该目录下的路径"
                "（可使用相对路径，会相对该目录解析）。\n"
                "2. 不要访问、列出、读取或修改该目录之外的任何路径，包括其他账号的"
                "工作区、系统配置、密钥与备份目录；即使你在日志、报错、示例或历史"
                "对话中见过这些路径，也不要尝试。\n"
                "3. 若确实需要使用上述目录之外的文件，向用户说明原因，由用户自行"
                "处理或改用其他方式，不要绕过限制。\n"
                "4. 其他账号的数据对你不可见是**预期行为**：如果用户要求查看别人"
                "的文件或数据，请说明系统按账号隔离、你无法访问，而不要反复尝试"
                "或猜测路径。"
            )
        return (
            "# Account Data Boundary\n\n"
            f"This is a multi-user system. You may only access files inside the "
            f"**current account's** workspace: `{root}`\n\n"
            "1. Use paths under that directory for every file read/write, search "
            "and command (relative paths resolve against it).\n"
            "2. Do not access, list, read or modify anything outside it — other "
            "accounts' workspaces, system configuration, secret and backup "
            "directories — even if you have seen such a path in a log, an error "
            "message, an example, or earlier conversation.\n"
            "3. If a file outside that boundary is genuinely needed, explain why "
            "and let the user handle it, or find another approach. Do not work "
            "around the restriction.\n"
            "4. Other accounts' data being invisible to you is **expected**: if "
            "asked to look at another account's files, say the system isolates "
            "accounts and you cannot, instead of retrying or guessing paths."
        )


class AgentsMdContributor(SyncPromptContributor):
    """Load ``AGENTS.md`` with heartbeat / memory section processing."""

    name = "agents_md"
    priority = 10

    def contribute_sync(self, ctx: "HookContext") -> str | None:
        wd = getattr(ctx, "workspace_dir", None)
        if not wd:
            return None
        content = _read_prompt_file(Path(wd), "AGENTS.md")
        if not content:
            return None
        extras = getattr(ctx, "extras", {}) or {}
        heartbeat_enabled = extras.get("heartbeat_enabled", False)
        try:
            content = _process_heartbeat_section(content, heartbeat_enabled)
        except Exception as e:
            logger.warning("Failed to process heartbeat: %s", e)
        memory_manager = extras.get("memory_manager")
        try:
            content = _process_memory_section(
                content,
                memory_manager,
            )
        except Exception as e:
            logger.warning("Failed to process memory section: %s", e)
        if not content:
            return None
        return f"# AGENTS.md\n\n{content}"


class SoulMdContributor(SyncPromptContributor):
    """Load ``SOUL.md``."""

    name = "soul_md"
    priority = 20

    def contribute_sync(self, ctx: "HookContext") -> str | None:
        wd = getattr(ctx, "workspace_dir", None)
        if not wd:
            return None
        content = _read_prompt_file(Path(wd), "SOUL.md")
        if not content:
            return None
        return f"# SOUL.md\n\n{content}"


class ProfileMdContributor(SyncPromptContributor):
    """Load ``PROFILE.md``."""

    name = "profile_md"
    priority = 30

    def contribute_sync(self, ctx: "HookContext") -> str | None:
        wd = getattr(ctx, "workspace_dir", None)
        if not wd:
            return None
        content = _read_prompt_file(Path(wd), "PROFILE.md")
        if not content:
            return None
        return f"# PROFILE.md\n\n{content}"


class WorkspacePromptFilesContributor(SyncPromptContributor):
    """Load configured workspace markdown files in UI-configured order."""

    name = "workspace_prompt_files"
    priority = 10

    def contribute_sync(self, ctx: "HookContext") -> str | None:
        wd = getattr(ctx, "workspace_dir", None)
        if not wd:
            return None

        workspace_dir = Path(wd)
        extras = getattr(ctx, "extras", {}) or {}
        parts: list[str] = []
        for filename in _system_prompt_files(ctx):
            content = _read_prompt_file(workspace_dir, filename)
            if not content:
                continue
            if filename == "AGENTS.md":
                content = self._process_agents_md(content, extras)
            if not content:
                continue
            parts.append(f"# {filename}\n\n{content}")
        return "\n\n".join(parts) or None

    @staticmethod
    def _process_agents_md(content: str, extras: dict[str, Any]) -> str:
        heartbeat_enabled = extras.get("heartbeat_enabled", False)
        try:
            content = _process_heartbeat_section(content, heartbeat_enabled)
        except Exception as e:
            logger.warning("Failed to process heartbeat: %s", e)
        memory_manager = extras.get("memory_manager")
        try:
            content = _process_memory_section(
                content,
                memory_manager,
            )
        except Exception as e:
            logger.warning("Failed to process memory section: %s", e)
        return content


class MultimodalHintContributor(SyncPromptContributor):
    """Inject multimodal capability awareness hint."""

    name = "multimodal_hint"
    priority = 80

    def contribute_sync(self, ctx: "HookContext") -> str | None:
        from ..agents.prompt import build_multimodal_hint

        hint = build_multimodal_hint()
        return hint or None


class ConversationScopeContributor(SyncPromptContributor):
    """Keep prior context and current-turn inputs distinct."""

    name = "conversation_scope"
    priority = 82

    def contribute_sync(self, ctx: "HookContext") -> str | None:
        del ctx
        return (
            "# Conversation Scope\n\n"
            "Treat the latest real external user message as the current request. "
            "Earlier user messages and assistant replies are background only; "
            "do not carry their attachments, assumptions, or unfinished claims "
            "into an unrelated request unless the user explicitly refers to them.\n\n"
            "Visual pages named `visual_context` or `visual_history` are internal "
            "QwenPaw context snapshots generated from earlier turns. They are "
            "not images uploaded or shown by the user in the current turn. Never "
            "say that the user just showed or provided such a page. Describe an "
            "image only when the current user message actually contains native "
            "image media or explicitly asks about an earlier image. Previous "
            "assistant answers are not evidence and must not be treated as facts."
        )


class FileDeliveryContributor(SyncPromptContributor):
    """Require user-facing delivery for files created during a chat."""

    name = "file_delivery"
    priority = 84

    def contribute_sync(self, ctx: "HookContext") -> str | None:
        language = (getattr(ctx, "extras", {}) or {}).get("language", "en")
        if language == "zh":
            return (
                "# 文件交付\n\n"
                "当当前对话的任务产出供用户使用的文件（例如 Word、Excel、"
                "PDF、PPT、CSV、图片、压缩包或报告）时，完成文件生成后必须将"
                "文件交付到当前聊天界面：调用 `send_file_to_user(file_path=...)`。"
                "不要只在回复中写服务器路径、工作区路径或文件名。只有工具结果已经"
                "明确附带了该文件时，才不要重复发送。内部脚本、临时文件和中间产物"
                "不要发送。"
            )
        return (
            "# File Delivery\n\n"
            "When the current chat task produces a user-facing file (for example "
            "Word, Excel, PDF, PowerPoint, CSV, image, archive, or report), "
            "deliver it in the current chat after generation by calling "
            "`send_file_to_user(file_path=...)`. Never merely state a server "
            "path, workspace path, or filename. Do not send it again only when "
            "a tool result already explicitly attached that file. Do not send "
            "internal scripts, temporary files, or intermediate artifacts."
        )


class CodingModeContributor(SyncPromptContributor):
    """Inject Coding Mode persona block when coding mode is active."""

    name = "coding_mode"
    priority = 85

    def contribute_sync(self, ctx: "HookContext") -> str | None:
        extras = getattr(ctx, "extras", {}) or {}
        agent_config = extras.get("agent_config")
        if agent_config is None:
            return None
        cm = getattr(agent_config, "coding_mode", None)
        if not cm or not getattr(cm, "enabled", False):
            return None
        from ..modes.coding import _CODING_SYSTEM_PROMPT_TEMPLATE

        workspace_dir = str(getattr(ctx, "workspace_dir", "") or "(unknown)")
        project_dir = self._resolve_project_dir(agent_config) or workspace_dir
        return _CODING_SYSTEM_PROMPT_TEMPLATE.format(
            project_dir=project_dir,
            workspace_dir=workspace_dir,
        )

    @staticmethod
    def _resolve_project_dir(agent_config: Any) -> str | None:
        """Prefer request config, then reload disk config for API switches."""
        project_dir = getattr(agent_config, "project_dir", None)
        if project_dir:
            return project_dir

        from ..config.config import load_agent_config

        agent_id = getattr(agent_config, "id", None)
        if not agent_id:
            return None
        try:
            fresh = load_agent_config(agent_id)
            if fresh.project_dir:
                return fresh.project_dir
        except Exception:
            logger.debug(
                "Failed to reload agent config for Coding Mode prompt",
                exc_info=True,
            )
        return None


class ScrollContextContributor(SyncPromptContributor):
    """Inject memory/recall guidance when the scroll context strategy is on."""

    name = "scroll_context"
    priority = 86

    def contribute_sync(self, ctx: "HookContext") -> str | None:
        extras = getattr(ctx, "extras", {}) or {}
        agent_config = extras.get("agent_config")
        if agent_config is None:
            return None
        try:
            strategy = agent_config.running.light_context_config.strategy
        except Exception:
            return None
        if strategy != "scroll":
            return None
        from ..agents.context.scroll.prompt import build_scroll_system_prompt

        language = getattr(agent_config, "language", "en")
        return build_scroll_system_prompt(language)


class KnowledgeRetrievalContributor(SyncPromptContributor):
    """Tell agents when the local knowledge tool is appropriate."""

    name = "knowledge_retrieval"
    priority = 87

    def contribute_sync(self, ctx: "HookContext") -> str | None:
        del ctx
        if not os.environ.get("QWENPAW_KNOWLEDGE_URL", "").strip():
            return None
        return (
            "# Local Knowledge\n\n"
            "When the user asks about uploaded business materials, historical "
            "documents, people, events, relationships, or reference writing, "
            "use the knowledge retrieval tools before answering.\n\n"
            "## Available Tools\n\n"
            "### query_knowledge\n\n"
            "Search for relevant documents in knowledge spaces. Returns a list "
            "of documents and snippet counts, but NOT the actual content.\n\n"
            "**Parameters:**\n"
            "- `query`: Search query (required)\n"
            "- `space_name`: Filter by space name (optional)\n\n"
            "**Returns:**\n"
            "```\n"
            "知识空间：{space_name}\n"
            "找到 N 个相关文档，共 M 个相关片段：\n"
            "[1] filename.pdf（X 个相关片段）\n"
            "    文档ID: {doc_id}\n"
            "...\n"
            "```\n\n"
            "### get_document_detail\n\n"
            "Get detailed content of a specific document. Use this after "
            "query_knowledge to retrieve actual content.\n\n"
            "**Parameters:**\n"
            "- `document_id`: Document ID from query_knowledge results (required)\n"
            "- `page_start`: Start page number, 0 means no filter (optional)\n"
            "- `page_end`: End page number, 0 means no filter (optional)\n"
            "- `max_chunks`: Maximum chunks to return, default 20 (optional)\n\n"
            "## Workflow\n\n"
            "1. Call `query_knowledge` to find relevant documents\n"
            "2. Analyze the results to identify which documents are relevant\n"
            "3. Call `get_document_detail` for specific documents you need\n"
            "4. Use the retrieved content to answer the user's question\n"
            "5. Cite filenames and page numbers naturally\n\n"
            "**IMPORTANT:**\n"
            "- Do not claim no relevant materials exist unless query_knowledge returns no documents\n"
            "- You MUST call get_document_detail to get actual content before answering\n"
            "- Use the evidence to answer the user's question based on your own reasoning\n"
            "- If the evidence is insufficient, say so clearly; do not make up information\n\n"
            "## Data Analysis + Knowledge Collaboration\n\n"
            "When querying data tables (via `query_data_table`), if the user's "
            "question contains business jargon, acronyms, metric names, or "
            "terminology you are not confident about, search the knowledge base "
            "first to understand the correct definition or calculation logic "
            "before writing the SQL query. For example: if a column is called "
            "「核心客户」and the user filters by it, check if the knowledge base "
            "has a definition of what counts as a core customer. Do NOT look up "
            "knowledge for every query — only when the terms are genuinely "
            "ambiguous or unfamiliar to you."
            "\n\n## Saving an Uploaded File\n\n"
            "When the user asks to keep an uploaded document for later (存到知识空间 / "
            "上传到知识库 / 以后能检索), use:\n\n"
            "### create_knowledge_space\n\n"
            "Create a named space. If one with the same name already exists the "
            "tool reuses it, so calling it is safe.\n\n"
            "### upload_knowledge_document\n\n"
            "Upload one file into a space, or into the user's default space when "
            "`space_name` is empty. The attachment file name is enough.\n\n"
            "**IMPORTANT:**\n"
            "- Only upload when the user asks you to keep the file. Reading a file "
            "to answer a question requires no upload.\n"
            "- Do not upload on your own initiative.\n"
        )


class DataAnalysisContributor(SyncPromptContributor):
    """Tell agents about the available data analysis tools."""

    name = "data_analysis"
    priority = 86

    def contribute_sync(self, ctx: "HookContext") -> str | None:
        del ctx
        return (
            "# Data Analysis\n\n"
            "You have access to data analysis tools that query structured "
            "business data (CSV/Excel uploads stored as database tables).\n\n"
            "## When to Use (unified trigger — read first)\n\n"
            "ONLY use these tools when the user's message contains the exact "
            "phrase「通过数据分析」. This is the ONLY trigger. Do NOT use "
            "these tools for any other phrasing — not 「数据分析XX」, not "
            "「用数据XX」, not 「查XX数据」, not 「统计XX」, not any "
            "variation. Only the exact phrase「通过数据分析」activates "
            "these tools.\n\n"
            "When triggered, call `list_data_tables` to find the matching "
            "table, then `query_data_table` to produce the answer with SQL.\n\n"
            "If the user does NOT say「通过数据分析」, DO NOT invoke these "
            "tools. Answer normally using the knowledge base or general "
            "knowledge.\n\n"
            "## Available Tools\n\n"
            "### list_data_tables\n\n"
            "List all uploaded data tables with row counts and column info.\n"
            "Call this first to see what data is available.\n\n"
            "### describe_data_table\n\n"
            "Show column definitions (name, type, description) for a specific "
            "table.\n\n"
            "**Parameters:**\n"
            "- `table_name`: Table name (required)\n\n"
            "### query_data_table\n\n"
            "Execute a SQL query against a data table. Returns query results "
            "in a readable format.\n\n"
            "**Parameters:**\n"
            "- `sql`: SQL query (required). Only SELECT statements are allowed.\n"
            "  Table name is available in the FROM clause as the registered "
            "  table name (use the exact name from list_data_tables).\n\n"
            "## Workflow\n\n"
            "1. Call `list_data_tables` to see what data is available\n"
            "2. Call `describe_data_table` to understand the column structure\n"
            "3. Write a SELECT SQL query and call `query_data_table`\n"
            "4. Present the results in a clear, readable format\n\n"
            "**Tips:**\n"
            "- Use Chinese column names as-is in SQL (they are stored as-is)\n"
            "- If a query returns too many rows, add LIMIT and ORDER BY\n"
            "- For aggregations, use GROUP BY with aggregate functions\n\n"
            "## Saving an Uploaded Table (write tools)\n\n"
            "Three tools persist a tabular file the user attached in this chat:\n\n"
            "### create_data_table\n\n"
            "Create a table definition. Pass only a Chinese description — column "
            "names and types are inferred from the file header during import, so "
            "do NOT ask the user for a column list.\n\n"
            "### import_data_table\n\n"
            "Import a CSV/XLSX/XLS file into an existing table. Pass `table_name` "
            "and `file` (the attachment file name is enough; the file is located "
            "automatically inside the user's workspace).\n\n"
            "### move_data_table_to_path\n\n"
            "File an already-existing table into a case, or move it back to the "
            "default area. Use this when the user says 把这张表放到案件X下 /\n"
            "归到X案件.\n\n"
            "## Cases / 案件 (paths)\n\n"
            "Data tables and knowledge documents can live in the account's default "
            "area or inside a 案件. `list_paths` shows the cases you may use; call "
            "it before naming one. The write tools above — plus\n"
            "`upload_knowledge_document` — accept a `path` argument that takes "
            "either the case name or its id.\n\n"
            "**Rules for `path`:**\n"
            "- Leave it empty unless the user says where the data should go. An "
            "empty value means the default area and is always correct.\n"
            "- Never invent a case name. If the user names one that `list_paths` "
            "does not show, say so and ask; do not pick a similar case.\n"
            "- If a name matches several cases the tool returns the candidates — "
            "ask the user which one instead of retrying with a guess.\n"
            "- Creating a new case is a user action: tell the user to add it in "
            "the 「数据分析」page and mount it to this agent, rather than trying "
            "to create it yourself.\n\n"
            "**Use them only when BOTH hold:**\n"
            "1. a tabular file is attached to this message (or the user points "
            "explicitly at a tabular file in their workspace), AND\n"
            "2. the user asks you to keep it — 建表 / 入库 / 存起来 / 以后能查 /\n"
            "   整理成表. Create the table first, then import.\n\n"
            "**Do NOT create a table when:**\n"
            "- the user only asks you to look at, summarise or analyse the data. "
            "Answer in the conversation instead, and you may offer once at the "
            "end: 要不要我把它建成数据表，以后可以直接查询？\n"
            "- no file is involved at all.\n\n"
            "**Notes:**\n"
            "- Chat attachments are capped at 20MB and their extracted text is "
            "truncated. For larger files, tell the user to import from the "
            "「数据分析」page, which accepts up to 10GB.\n"
            "- After a successful import, keep answering with `query_data_table`.\n"
        )


class EnvContextContributor(SyncPromptContributor):
    """Append the environment context block (time / session / OS)."""

    name = "env_context"
    priority = 90

    def contribute_sync(self, ctx: "HookContext") -> str | None:
        extras = getattr(ctx, "extras", {}) or {}
        return extras.get("env_context") or None


class DriverPolicyHintContributor(SyncPromptContributor):
    """Append request-time Driver policy guidance when tools are exposed."""

    name = "driver_policy_hint"
    priority = 88

    def contribute_sync(self, ctx: "HookContext") -> str | None:
        extras = getattr(ctx, "extras", {}) or {}
        hints = extras.get("driver_prompt_hints") or []
        rendered = "\n\n".join(str(hint) for hint in hints if hint)
        return rendered or None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_ALL_CONTRIBUTORS = (
    AgentIdentityContributor,
    AccountBoundaryContributor,
    WorkspacePromptFilesContributor,
    MultimodalHintContributor,
    ConversationScopeContributor,
    FileDeliveryContributor,
    CodingModeContributor,
    ScrollContextContributor,
    KnowledgeRetrievalContributor,
    DataAnalysisContributor,
    DriverPolicyHintContributor,
    EnvContextContributor,
)


def build_default_prompt_manager() -> PromptManager:
    """Create a :class:`PromptManager` with all built-in contributors."""
    pm = PromptManager()
    for cls in _ALL_CONTRIBUTORS:
        pm.register(cls())
    return pm


__all__ = [
    "AgentIdentityContributor",
    "AccountBoundaryContributor",
    "AgentsMdContributor",
    "SoulMdContributor",
    "ProfileMdContributor",
    "WorkspacePromptFilesContributor",
    "MultimodalHintContributor",
    "ConversationScopeContributor",
    "FileDeliveryContributor",
    "CodingModeContributor",
    "ScrollContextContributor",
    "KnowledgeRetrievalContributor",
    "DataAnalysisContributor",
    "DriverPolicyHintContributor",
    "EnvContextContributor",
    "build_default_prompt_manager",
]
