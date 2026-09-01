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
            "## When to Use (explicit trigger — read first)\n\n"
            "ONLY use these tools when the user EXPLICITLY asks to use the "
            "data analysis feature, for example: 「用数据分析分析XX」"
            "「通过数据分析中的数据查XX」「在数据分析里看XX表」"
            "「用SQL查XX」or any phrasing that explicitly names the data "
            "analysis feature or its tables. When such an explicit trigger "
            "is present, call `list_data_tables` to find the matching table, "
            "then `query_data_table` to produce the answer with SQL.\n\n"
            "If the user does NOT explicitly mention data analysis, DO NOT "
            "invoke these tools on your own — do not guess that a question "
            "is about data analysis just because it mentions numbers, "
            "statistics, or the word 「数据」. In that case answer normally "
            "(the knowledge base tools remain available for document "
            "questions). Only the explicit trigger authorizes data analysis.\n\n"
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
            "- For aggregations, use GROUP BY with aggregate functions\n"
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
    WorkspacePromptFilesContributor,
    MultimodalHintContributor,
    ConversationScopeContributor,
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
    "AgentsMdContributor",
    "SoulMdContributor",
    "ProfileMdContributor",
    "WorkspacePromptFilesContributor",
    "MultimodalHintContributor",
    "ConversationScopeContributor",
    "CodingModeContributor",
    "ScrollContextContributor",
    "KnowledgeRetrievalContributor",
    "DataAnalysisContributor",
    "DriverPolicyHintContributor",
    "EnvContextContributor",
    "build_default_prompt_manager",
]
