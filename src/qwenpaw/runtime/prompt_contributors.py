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


class WorkspaceLayoutContributor(SyncPromptContributor):
    """Force generated artifacts and helper code into fixed subdirectories."""

    name = "workspace_layout"
    priority = 83

    def contribute_sync(self, ctx: "HookContext") -> str | None:
        workspace = str(getattr(ctx, "workspace_dir", "") or "").rstrip("/\\")
        if not workspace:
            return None
        language = (getattr(ctx, "extras", {}) or {}).get("language", "en")
        if language == "zh":
            return (
                "# 工作区文件规范（强制）\n\n"
                f"当前工作区：`{workspace}`。所有新文件必须按下面两个目录存放，"
                "没有例外：\n\n"
                "1. `outputs/`：所有交付给用户的生成结果，包括报告、Word、Excel、"
                "PDF、PPT、CSV、图片、图表、压缩包和其他导出文件。\n"
                "2. `scripts/`：为完成任务生成的中间代码和脚本，包括 Python、"
                "SQL、Shell、JavaScript 等；需要复用时也放在这里。\n"
                "3. `outputs/tmp/`：临时中间文件，任务结束前清理。\n\n"
                "强制规则：\n"
                "- 禁止在工作区根目录新建任何文件，包括 `gen_*.js`、"
                "`generate_*.js`、临时 `.py`/`.sql`/`.sh`、CSV、文档和图片。\n"
                "- 禁止写入 `upload/`、`media/`、`chats.json`、`credentials.yaml` "
                "或工作区里的配置/日志文件。\n"
                "- 生成结果统一命名为 `outputs/<有意义的文件名>`；中间代码统一为 "
                "`scripts/<有意义的文件名>`。\n"
                "- 不要为每个小步骤新建脚本；优先复用一个脚本并在 `scripts/` "
                "下迭代。\n"
                "- 不要在 `execute_shell_command` 里用 heredoc（如 "
                "`python3 - <<'PY'`）、`python -c` 或 `psql -c` 执行多行代码；"
                "先用 `write_file` 写到 `scripts/<name>.py`，再执行 "
                "`python3 scripts/<name>.py`。\n"
                "- 调用 `send_file_to_user` 时只发送 `outputs/` 下的最终文件；"
                "除非用户明确要求，不要发送 `scripts/` 里的代码。\n"
                "- 如果发现自己已经在根目录生成了文件，立即移动到 `outputs/`"
                "（结果）或 `scripts/`（代码），并在回复中报告最终路径。\n"
                "- 这条规则优先于任何“临时放一下”“先写这里”的考虑；即使工具"
                "允许写根目录，也不允许这样做。"
            )
        return (
            "# Workspace Layout (mandatory)\n\n"
            f"Workspace: `{workspace}`. Every new file must go into one of these "
            "two directories — no exceptions:\n\n"
            "1. `outputs/`: all user-facing results, including reports, Word, "
            "Excel, PDF, PowerPoint, CSV, images, charts, archives and exports.\n"
            "2. `scripts/`: intermediate code and scripts used to produce the "
            "result, including Python, SQL, Shell and JavaScript.\n"
            "3. `outputs/tmp/`: temporary intermediate files; clean them up "
            "before finishing.\n\n"
            "Mandatory rules:\n"
            "- Never create files in the workspace root, including `gen_*.js`, "
            "`generate_*.js`, temporary `.py`/`.sql`/`.sh`, CSV, documents or "
            "images.\n"
            "- Never write into `upload/`, `media/`, `chats.json`, "
            "`credentials.yaml`, or workspace configuration/log files.\n"
            "- Name results `outputs/<meaningful-name>` and helper code "
            "`scripts/<meaningful-name>`.\n"
            "- Do not create a new script for every small step; reuse one script "
            "under `scripts/` and iterate on it.\n"
            "- Do not run multi-line code through a shell heredoc "
            "(`python3 - <<'PY'`), `python -c`, or `psql -c`; write it to "
            "`scripts/<name>.py` with `write_file`, then run "
            "`python3 scripts/<name>.py`.\n"
            "- Call `send_file_to_user` only with final files under `outputs/`; "
            "do not send `scripts/` code unless the user explicitly asks for it.\n"
            "- If you already created a root-level file, move it immediately to "
            "`outputs/` (result) or `scripts/` (code) and report the final path.\n"
            "- This rule overrides convenience or “temporary” placement; even if "
            "a tool allows writing to the root, you must not do so."
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
            "上传到知识库 / 以后能检索文档), use `upload_knowledge_document`.\n\n"
            "**Tabular files are different.** If the attachment is CSV/XLSX/XLS and "
            "the user wants it as a queryable table — 建表 / 入库 / 导入 / 录入 / "
            "存到数据空间 / 以后用 SQL 查 — do NOT use "
            "`upload_knowledge_document`. Use the data-table write tools "
            "(`create_data_table`, then `import_data_table`). Uploading a "
            "spreadsheet to the knowledge space only makes it searchable as a "
            "document; it does not create a queryable data table.\n\n"
            "- If the user says「上传」but names 数据空间/数据表/建表/入库, treat it "
            "as a data-table import, not a knowledge upload.\n"
            "- If the user only says「上传」and the intent is genuinely ambiguous, "
            "ask one short question: 是要建成可查询的数据表，还是存进知识空间做文档检索？\n"
            "- If the user names a knowledge-space folder/path (例如"
            "「反诈宣传知识库」), pass it as `path`. A missing folder is created "
            "automatically and granted to the current agent, so the document "
            "appears under that folder in the console rather than in the public "
            "area.\n"
            "- If the user does not name a folder, leave `path` empty; the "
            "document goes to the default/public area.\n"
            "- Do not guess a folder when the request is ambiguous. Ask the user "
            "to confirm the target folder instead.\n"
            "- An existing folder is reused; do not create a duplicate name.\n"
            "- `create_knowledge_space` is only for a named service-level space. "
            "It does not replace the console folder; use `path` for what the user "
            "sees in the knowledge-space tree.\n\n"
            "**IMPORTANT:**\n"
            "- Only upload when the user asks you to keep the file for document "
            "retrieval. Reading a file to answer a question requires no upload.\n"
            "- Do not upload on your own initiative.\n"
            "- Never use `upload_knowledge_document` as a fallback when the user "
            "wanted a data table; report the data-path problem and use the "
            "data-table tools instead.\n"
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
            "## When to Use\n\n"
            "### Reading / querying existing tables\n\n"
            "ONLY use `list_data_tables`, `describe_data_table`, and "
            "`query_data_table` when the user's message contains the exact "
            "phrase「通过数据分析」. This is the ONLY trigger for the read/query "
            "tools. Do NOT use them for 「数据分析XX」, 「用数据XX」, "
            "「查XX数据」, 「统计XX」, or any other variation.\n\n"
            "When triggered, call `list_data_tables` to find the matching "
            "table, then `query_data_table` to produce the answer with SQL.\n\n"
            "### Creating / importing a table (write tools)\n\n"
            "The write tools `create_data_table`, `import_data_table`, and "
            "`move_data_table_to_path` are NOT gated by「通过数据分析」. Use them "
            "when BOTH hold:\n"
            "1. a tabular file (CSV/XLSX/XLS) is attached to this message, or "
            "the user explicitly points at one in their workspace; AND\n"
            "2. the user asks to keep it as a data table — 建表 / 入库 / 导入 / "
            "录入 / 存到数据空间 / 整理成表 / 以后用 SQL 查.\n\n"
            "The user does not need to say「通过数据分析」for these write tools. "
            "If the user says 数据空间/数据表/建表/入库/导入/录入, that is enough. "
            "**Never use `upload_knowledge_document` for a CSV/XLSX/XLS that the "
            "user wants as a data table** — knowledge space is for document "
            "retrieval, data space is for structured tables. If the intent is "
            "genuinely ambiguous, ask one short question.\n\n"
            "If the user did NOT ask to create/import a table and did NOT say "
            "「通过数据分析」, answer normally using the knowledge base or "
            "general knowledge; do not invoke the data tools.\n\n"
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
            "Workflow when the user wants the attached tabular file kept:\n"
            "1. If the user named a target path, call `list_paths` to see what is "
            "mounted.\n"
            "2. Call `create_data_table` with a Chinese description and `path` "
            "(leave `path` empty for the default area).\n"
            "3. Call `import_data_table` with the returned `table_name` and the "
            "attachment file name.\n"
            "4. Only use `move_data_table_to_path` afterwards if the user wants "
            "an existing table moved into a case.\n\n"
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
            "If the named path is not mounted, do not fall back to the knowledge "
            "space and do not silently drop the request: either create/import the "
            "table in the default area and tell the user it can be moved after "
            "mounting, or tell the user to mount the path first. Never upload a "
            "CSV/XLSX/XLS to `upload_knowledge_document` as a substitute.\n\n"
            "## Cases / 案件 (paths)\n\n"
            "Data tables and knowledge documents can live in the account's default "
            "area or inside a 案件. `list_paths` shows only the paths mounted to "
            "this agent; call it before naming one. The write tools above accept "
            "a `path` argument that takes either the case name or its id.\n\n"
            "**Rules for `path`:**\n"
            "- Leave it empty unless the user says where the data should go. An "
            "empty value means the default area and is always correct.\n"
            "- `list_paths` is not the account's full path list. A name it does "
            "not show may still exist in the account but not be mounted to this "
            "agent. Do NOT claim it does not exist, do NOT pick a similar case, "
            "and do NOT fall back to `upload_knowledge_document`.\n"
            "- If the tool says the path exists but is not mounted, relay that "
            "exactly: ask the user to mount it in the 「数据分析」page, or create "
            "the table in the default area now and use "
            "`move_data_table_to_path` after it is mounted.\n"
            "- If a name matches several cases the tool returns the candidates — "
            "ask the user which one instead of retrying with a guess.\n"
            "- Creating a new case is a user action: tell the user to add it in "
            "the 「数据分析」page and mount it to this agent, rather than trying "
            "to create it yourself.\n\n"
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


class UploadInboxContributor(SyncPromptContributor):
    """Explain the shared `upload/` inbox and how to inspect large inputs."""

    name = "upload_inbox"
    priority = 87

    def contribute_sync(self, ctx: "HookContext") -> str | None:
        language = (getattr(ctx, "extras", {}) or {}).get("language", "en")
        workspace_dir = str(getattr(ctx, "workspace_dir", "") or "").rstrip(
            "/\\",
        )
        # Relative paths resolve against the turn's project directory, which
        # may differ from the agent workspace. Give the absolute inbox path so
        # the model does not have to guess.
        inbox_path = f"{workspace_dir}/upload" if workspace_dir else "upload/"
        if language == "zh":
            return (
                "# 上传目录 upload/\n\n"
                "用户通过文件页面上传的文件统一保存在当前工作区的 `upload/` "
                f"目录（绝对路径：`{inbox_path}`，文件 → 工作区 → upload）。"
                "zip、tar、tar.gz、tgz、gz "
                "压缩包上传后由服务器自动解压到 `upload/` 下的同名目录；解压成功后"
                "原压缩包会自动删除，解压失败则保留；7z、rar 等格式不会自动解压。"
                "\n\n"
                "分析上传内容时：\n"
                "- 先列出目录结构、统计文件数量与类型，再抽样读取；不要试图一次"
                "读完整棵树或整个大文件。\n"
                "- 单文件读取有大小上限。大文件用脚本按块或按行处理，或先读取"
                "表头、样例行和统计信息。\n"
                "- 需要查找内容时优先在服务端搜索（grep 或脚本），只把命中的"
                "少量结果带回上下文。\n"
                "- 解压失败、目录或文件不存在时，如实告知用户，不要猜测内容。\n"
                "- `upload/` 只属于当前账号；不要尝试访问其他账号的路径。\n\n"
                "如果用户明确要求清理或删除上传的文件，使用 "
                "`delete_upload_files`：\n"
                "- 先用 `dry_run=true`（默认）列出将删除的项与释放空间；\n"
                "- 范围确认后再用 `dry_run=false` 执行；\n"
                "- 删除非空目录需同时传 `recursive=true`；\n"
                "- 该工具只能删除当前账号 `upload/` 下的内容，不要声称删除了"
                "其他路径；\n"
                "- 执行后如实报告删除数量、释放空间和失败项。"
            )
        return (
            "# Upload inbox `upload/`\n\n"
            "Files uploaded through the Files page all live under the current "
            "workspace's `upload/` directory (absolute path: "
            f"`{inbox_path}`; Files → Workspace → upload). "
            "zip, tar, tar.gz, tgz, and gz archives are extracted by the server "
            "into a sibling directory with the same name; a successfully "
            "extracted archive is deleted, while a failed one is kept. 7z, "
            "rar, and other formats are not extracted.\n\n"
            "When analyzing uploaded content:\n"
            "- List the tree and summarize file counts and types first, then "
            "sample; never try to read the whole tree or an entire large file "
            "in one go.\n"
            "- Single-file reads have a size limit. Process large files with "
            "scripts in chunks or line ranges, or read headers, sample rows, "
            "and statistics first.\n"
            "- For lookups, search on the server (grep or a script) and bring "
            "back only the few matching results.\n"
            "- If extraction failed or a directory/file is missing, tell the "
            "user plainly; do not guess its contents.\n"
            "- `upload/` belongs to the current account; never attempt to "
            "access another account's paths.\n\n"
            "When the user explicitly asks to clean up or delete uploaded "
            "files, use `delete_upload_files`:\n"
            "- Start with `dry_run=true` (the default) to list what would be "
            "deleted and how much space would be freed;\n"
            "- Run again with `dry_run=false` after the scope is confirmed;\n"
            "- Deleting a non-empty directory also needs `recursive=true`;\n"
            "- The tool only reaches this account's `upload/`; never claim to "
            "have deleted anything else;\n"
            "- Report the real deleted count, freed space and failures."
        )


class ToolHardConstraintsContributor(SyncPromptContributor):
    """State the hard tool limits that approval cannot override."""

    name = "tool_hard_constraints"
    priority = 87

    def contribute_sync(self, ctx: "HookContext") -> str | None:
        language = (getattr(ctx, "extras", {}) or {}).get("language", "en")
        if language == "zh":
            return (
                "# 工具硬约束（审批不能绕过）\n\n"
                "下面这些限制是执行层强制的，**用户点同意也不会放行**。不要为了这些操作去触发审批，"
                "也不要重复尝试同一条被拒绝的命令：\n"
                "- 不读其他账号、`working.secret`、应用源码、`/proc`、系统密钥；\n"
                "- 不写工作区根目录、`upload/`、`media/`；结果只写 `outputs/`，代码只写 `scripts/`；\n"
                "- 不直连网络、数据库 TCP 端口、知识服务端口或其他宿主端口；"
                "但这不是禁止外部数据，允许的方式见下。\n"
                "- 不起常驻服务、不监听端口、不后台 `nohup`。\n\n"
                "允许且推荐的数据访问方式：\n"
                "- 自己的 PostgreSQL：直接用标准 `PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE` "
                "环境变量 `psycopg2.connect()` 或 `psql`，不要自己写 host/port；\n"
                "- 第三方 PostgreSQL/MySQL/Oracle/Hive/HTTP：不要直连端口，"
                "用 `scripts/huizhi_gateway.py`：\n"
                "  `import sys, os; sys.path.insert(0, os.environ['HUIZHI_SCRIPTS_DIR']); "
                "import huizhi_gateway`\n"
                "  `huizhi_gateway.sources()` 列出已授权数据源；"
                "`huizhi_gateway.query(source, sql)` 只读查询；"
                "`huizhi_gateway.http(method, url)` 调用白名单 API；\n"
                "- 知识库/数据表：用 `query_knowledge`、`query_data_table` 等受控工具。\n"
                "- 数据源没配置：请管理员在 `working.secret/sandbox-external-sources.json` "
                "添加，不要尝试绕过网关直连。\n\n"
                "为减少审批，执行任务时遵守：\n"
                "- 不要用 shell 的 heredoc、`python -c`、`psql -c`；先写 `scripts/<name>.py`，"
                "再执行 `python3 scripts/<name>.py`；\n"
                "- 不要用 `rm`、`mv`、`chmod`、`chown`、`kill`、`sudo`、`curl`、`wget`、"
                "`pip install`、`apt/dnf`、`systemctl`、`reboot`、`ssh`、`scp`、`find -delete`；\n"
                "- 清理文件用 `delete_upload_files` 或 `scripts/` 里的 Python 脚本，不要用 shell `rm`；\n"
                "- 只执行 `python3 scripts/<name>.py`、`bash scripts/<name>.sh`、`ls`、`head`、"
                "`grep`、`wc` 这类简单命令；\n"
                "- 工具返回 `DENIED`、`Sandbox violation` 或要求审批时，**不要重试同一命令**，"
                "按提示改路径或改用受控工具（`query_data_table`、`query_knowledge`、"
                "`read_file`、`write_file`）。\n\n"
                "如果确实需要边界外的能力，向用户说明这是系统硬限制，由管理员处理；不要通过"
                "提示词、编码、路径拼接或审批来绕过。"
            )
        return (
            "# Tool hard constraints (approval cannot bypass)\n\n"
            "These limits are enforced by the execution layer. **Approving a prompt "
            "does not override them.** Do not trigger approvals for them and do not "
            "retry a denied command:\n"
            "- Do not read other accounts, `working.secret`, application source, "
            "`/proc`, or system keys;\n"
            "- Do not write the workspace root, `upload/`, or `media/`; results go "
            "only to `outputs/`, code only to `scripts/`;\n"
            "- Do not connect directly to the network, database TCP ports, knowledge "
            "service ports, or other host ports. This does not forbid external data; "
            "use the supported paths below;\n"
            "- Do not start long-running services, listeners, or `nohup` processes.\n\n"
            "Supported data access:\n"
            "- Your own PostgreSQL: use the standard `PGHOST/PGPORT/PGUSER/PGPASSWORD/"
            "PGDATABASE` variables with `psycopg2.connect()` or `psql`; never hard-code "
            "a host/port;\n"
            "- Third-party PostgreSQL/MySQL/Oracle/Hive/HTTP: do not connect to ports "
            "directly. Use `scripts/huizhi_gateway.py`:\n"
            "  `import sys, os; sys.path.insert(0, os.environ['HUIZHI_SCRIPTS_DIR']); "
            "import huizhi_gateway`\n"
            "  `huizhi_gateway.sources()` lists authorized sources; "
            "`huizhi_gateway.query(source, sql)` is read-only; "
            "`huizhi_gateway.http(method, url)` calls an allow-listed API;\n"
            "- Knowledge/data tables: use `query_knowledge`, `query_data_table`, etc.;\n"
            "- Missing source: ask the administrator to add it to "
            "`working.secret/sandbox-external-sources.json`; never bypass the gateway.\n\n"
            "To reduce approvals:\n"
            "- Do not use shell heredocs, `python -c`, or `psql -c`; write "
            "`scripts/<name>.py` first, then run `python3 scripts/<name>.py`;\n"
            "- Do not use `rm`, `mv`, `chmod`, `chown`, `kill`, `sudo`, `curl`, "
            "`wget`, `pip install`, `apt/dnf`, `systemctl`, `reboot`, `ssh`, `scp`, "
            "or `find -delete`;\n"
            "- Clean up with `delete_upload_files` or a Python script under `scripts/`, "
            "not shell `rm`;\n"
            "- Only run simple commands such as `python3 scripts/<name>.py`, "
            "`bash scripts/<name>.sh`, `ls`, `head`, `grep`, `wc`;\n"
            "- On `DENIED`, `Sandbox violation`, or an approval request, **do not retry "
            "the same command**; adjust the path or use a controlled tool "
            "(`query_data_table`, `query_knowledge`, `read_file`, `write_file`).\n\n"
            "If an out-of-bound capability is genuinely needed, explain that it is a "
            "hard system limit and let the administrator handle it; never bypass it "
            "through prompting, encoding, path tricks, or approval."
        )


class UserSandboxContributor(SyncPromptContributor):
    """Explain the ordinary-user shell sandbox and its writable output dir."""

    name = "user_sandbox"
    priority = 88

    def contribute_sync(self, ctx: "HookContext") -> str | None:
        outputs = ""
        scripts = ""
        try:
            from ..agents.tools.user_files import resolve_account_username
            from ..app.user_space import get_user_root, is_admin_user

            username = resolve_account_username()
            if is_admin_user(username):
                return None
            root_text = str(get_user_root(username).resolve()).rstrip("/\\")
            outputs = f"{root_text}/outputs"
            scripts = f"{root_text}/scripts"
        except Exception:  # noqa: BLE001 - prompt must not fail the turn
            pass
        if not outputs:
            workspace_text = str(getattr(ctx, "workspace_dir", "") or "").rstrip("/\\")
            outputs = f"{workspace_text}/outputs"
            scripts = f"{workspace_text}/scripts"
        language = (getattr(ctx, "extras", {}) or {}).get("language", "en")
        if language == "zh":
            return (
                "# 普通用户代码执行沙箱\n\n"
                "`execute_shell_command` 在隔离沙箱中运行，不是宿主机 shell：\n"
                f"- 可读：当前账号工作区（含 `media/`、`upload/`）和系统运行库，均**只读**。\n"
                f"- 可写：只有 `outputs/`（绝对路径 `{outputs}`）、`scripts/`"
                f"（绝对路径 `{scripts}`），以及 `outputs/tmp/`。\n"
                "- 无直接网络：不能访问互联网、局域网、数据库 TCP 端口、知识服务端口；"
                "自己的 PG 和外部数据用下面的受控方式。\n"
                "- 看不到：`/app`、`/workspace`、`working.secret`、其他账号目录、`/proc`。\n\n"
                "生成结果必须写到 `outputs/`，例如 "
                f"`{outputs}/result.csv`；中间代码写到 `scripts/`；不要尝试修改 "
                "`upload/`、`media/` 或工作区根目录。\n\n"
                "数据库：\n"
                "- 自己的 PostgreSQL 库已经通过标准 `PGHOST/PGUSER/PGPASSWORD/PGDATABASE` "
                "环境变量接好，可以直接 `psycopg2.connect()` / `psql`，在自己的库内可读写；"
                "但不能访问平台库或其他账号的库。\n"
                "- 第三方数据库/API：不要直连网络。使用 `scripts/huizhi_gateway.py`：\n"
                "  `import sys, os; sys.path.insert(0, os.environ['HUIZHI_SCRIPTS_DIR']); "
                "import huizhi_gateway`\n"
                "  `huizhi_gateway.sources()` 列出管理员配置的数据源；"
                "`query(source, sql)` 只读查询；`http(method, url)` 调用白名单 API。\n"
                "- 需要知识库时使用 `query_knowledge` 等受控工具。"
            )
        return (
            "# Ordinary-user shell sandbox\n\n"
            "`execute_shell_command` runs inside an isolated sandbox, not on the host:\n"
            "- Readable: this account's workspace (including `media/` and `upload/`) "
            "and the system runtime, all **read-only**.\n"
            f"- Writable: `outputs/` (absolute path `{outputs}`), `scripts/` "
            f"(absolute path `{scripts}`), and `outputs/tmp/`.\n"
            "- No direct network: no internet, LAN, database TCP ports, knowledge "
            "service ports, or host ports. This does not forbid external data; "
            "use the supported paths below.\n"
            "- Invisible: `/app`, `/workspace`, `working.secret`, other accounts, `/proc`.\n\n"
            "Supported data access:\n"
            "- Your own PostgreSQL: use the standard `PGHOST/PGPORT/PGUSER/PGPASSWORD/"
            "PGDATABASE` variables with `psycopg2.connect()` or `psql`; do not hard-code "
            "a host/port.\n"
            "- Third-party PostgreSQL/MySQL/Oracle/Hive/HTTP: do not connect to ports "
            "directly. Use `scripts/huizhi_gateway.py`:\n"
            "  `import sys, os; sys.path.insert(0, os.environ['HUIZHI_SCRIPTS_DIR']); "
            "import huizhi_gateway`\n"
            "  `huizhi_gateway.sources()` lists authorized sources; "
            "`huizhi_gateway.query(source, sql)` is read-only; "
            "`huizhi_gateway.http(method, url)` calls an allow-listed API.\n"
            "- Knowledge/data tables: use `query_knowledge`, `query_data_table`, etc.\n"
            "- If a source is missing, ask the administrator to add it to "
            "`working.secret/sandbox-external-sources.json`; never bypass the gateway.\n\n"
            f"Write results under `outputs/`, for example `{outputs}/result.csv`; "
            "write intermediate code under `scripts/`; do not modify `upload/`, "
            "`media/`, or the workspace root.\n\n"
            "Database:\n"
            "- Your own PostgreSQL database is wired through the standard "
            "`PGHOST/PGUSER/PGPASSWORD/PGDATABASE` variables: `psycopg2.connect()` "
            "or `psql` works, with read/write inside your own database only. "
            "Platform and other accounts' databases are not reachable.\n"
            "- Third-party databases/APIs: do not connect to the network directly. "
            "Use `scripts/huizhi_gateway.py`:\n"
            "  `import sys, os; sys.path.insert(0, os.environ['HUIZHI_SCRIPTS_DIR']); "
            "import huizhi_gateway`\n"
            "  `huizhi_gateway.sources()` lists administrator-configured sources; "
            "`query(source, sql)` is read-only; `http(method, url)` calls an "
            "allow-listed API.\n"
            "- Use `query_knowledge` for knowledge retrieval."
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
    WorkspaceLayoutContributor,
    ToolHardConstraintsContributor,
    FileDeliveryContributor,
    CodingModeContributor,
    ScrollContextContributor,
    KnowledgeRetrievalContributor,
    DataAnalysisContributor,
    UploadInboxContributor,
    UserSandboxContributor,
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
    "WorkspaceLayoutContributor",
    "ToolHardConstraintsContributor",
    "FileDeliveryContributor",
    "CodingModeContributor",
    "ScrollContextContributor",
    "KnowledgeRetrievalContributor",
    "DataAnalysisContributor",
    "UploadInboxContributor",
    "UserSandboxContributor",
    "DriverPolicyHintContributor",
    "EnvContextContributor",
    "build_default_prompt_manager",
]
