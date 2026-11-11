# -*- coding: utf-8 -*-
"""Agent tool: delete files inside the account's ``upload/`` inbox.

Deletion is deliberately confined to one directory: the inbox is the only
place where users upload arbitrary files, and it is the only place where an
agent is allowed to remove them.  Every path is re-validated by the service
layer, which also verifies the entry is gone before reporting success.
"""

import asyncio
import json
import logging
from typing import Any, Optional

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from ...config.context import get_current_workspace_dir
from ...runtime.tool_registry import tool_descriptor
from ...services import upload_inbox

logger = logging.getLogger(__name__)

_MAX_PATHS = 5000
_PREVIEW_LIMIT = 200


def _as_list(value: Any) -> list[str]:
    """Accept a real list, a JSON array string, or a comma-separated string."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [part.strip() for part in text.split(",") if part.strip()]
        if isinstance(parsed, (list, tuple)):
            return [str(item) for item in parsed]
        return [str(parsed)]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _human_bytes(size: int) -> str:
    if size <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{value:.0f} {units[index]}" if index == 0 else (
        f"{value:.1f} {units[index]}"
    )


def _result(text: str) -> ToolChunk:
    return ToolChunk(
        is_last=True,
        state=ToolResultState.SUCCESS,
        content=[TextBlock(type="text", text=text)],
    )


def _format_entries(rows: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for row in rows[:_PREVIEW_LIMIT]:
        kind = "目录" if row["kind"] == "directory" else "文件"
        missing = "" if row.get("exists", True) else "，不存在"
        lines.append(
            f"- {row['path']}（{kind}，{_human_bytes(row['bytes'])}"
            f"{missing}）",
        )
    if len(rows) > _PREVIEW_LIMIT:
        lines.append(f"- …共 {len(rows)} 项")
    return lines


def _render_preview(rows: list[dict[str, Any]]) -> str:
    total = sum(int(row["bytes"]) for row in rows)
    lines = [
        f"将删除 {len(rows)} 项，共 {_human_bytes(total)}"
        "（dry_run=true，尚未执行）：",
        *_format_entries(rows),
        "",
        "确认范围无误后，再次调用本工具并传 `dry_run=false` 才会真正删除。",
        "删除非空目录需要同时传 `recursive=true`。",
    ]
    return "\n".join(lines)


def _render_deletion(result: dict[str, Any]) -> str:
    deleted = result["deleted"]
    failed = result["failed"]
    lines = [
        f"已删除 {len(deleted)} 项，释放 {_human_bytes(result['freed_bytes'])}。",
    ]
    if deleted:
        lines.append("已删除：")
        lines.extend(f"- {path}" for path in deleted[:_PREVIEW_LIMIT])
        if len(deleted) > _PREVIEW_LIMIT:
            lines.append(f"- …共 {len(deleted)} 项")
    if failed:
        lines.append("")
        lines.append("未删除：")
        for item in failed[:50]:
            lines.append(f"- {item['path']}：{item['error']}")
        if len(failed) > 50:
            lines.append(f"- …共 {len(failed)} 项失败")
        lines.append("目录非空时请传 `recursive=true` 后重试。")
    return "\n".join(lines)


@tool_descriptor(
    tool_type="file",
    policy_name="DeleteUploadedFiles",
    default_policy="allow",
    policy_reason="Delete entries inside the account's upload inbox",
    ui_description="删除 upload/ 中的文件",
    ui_icon="🗑️",
)
async def delete_upload_files(
    paths: Optional[list[str] | str] = None,
    patterns: Optional[list[str] | str] = None,
    recursive: bool | str = False,
    dry_run: bool | str = True,
) -> ToolChunk:
    """Delete files or directories inside the current account's `upload/`.

    The tool can ONLY reach the caller's own `upload/` inbox; other workspace
    files are rejected. Use it when the user explicitly asks to clean up or
    delete uploaded files.

    Args:
        paths: Workspace-relative paths, each starting with `upload/`
            (e.g. `upload/tmp/a.log`). A JSON array string is also accepted.
        patterns: Glob rules relative to `upload/` (e.g. `*.tmp`,
            `logs/**/*.log`). `*` and `**` are supported. Matches are combined
            with `paths`.
        recursive: Allow deleting non-empty directories. Default false.
        dry_run: Default true: only preview what would be deleted. Call again
            with `dry_run=false` after the scope is confirmed.

    Returns:
        A text summary with deleted paths, failures and bytes freed.
    """
    workspace = get_current_workspace_dir()
    if workspace is None:
        return _result("Error: 无法确定当前工作区，已拒绝删除。")

    try:
        path_list = _as_list(paths)
        pattern_list = _as_list(patterns)
        if not path_list and not pattern_list:
            return _result("Error: 请提供 `paths` 或 `patterns`。")
        if pattern_list:
            path_list.extend(
                await asyncio.to_thread(
                    upload_inbox.expand_upload_patterns,
                    workspace,
                    pattern_list,
                    limit=_MAX_PATHS,
                ),
            )
        deduped: list[str] = []
        seen: set[str] = set()
        for item in path_list:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        if not deduped:
            return _result("没有匹配到 `upload/` 下的文件，未执行删除。")
        if len(deduped) > _MAX_PATHS:
            return _result(
                f"Error: 匹配项超过 {_MAX_PATHS} 个，请缩小删除规则。",
            )

        # Validates every path before any deletion happens.
        rows = await asyncio.to_thread(
            upload_inbox.describe_inbox_entries,
            workspace,
            deduped,
        )
        if _as_bool(dry_run):
            return _result(_render_preview(rows))

        result = await asyncio.to_thread(
            upload_inbox.delete_inbox_entries,
            workspace,
            deduped,
            recursive=_as_bool(recursive),
        )
    except upload_inbox.InboxUploadError as exc:
        return _result(f"Error: {exc}")
    except OSError as exc:
        logger.exception("agent upload deletion failed")
        return _result(f"Error: 删除失败：{exc}")

    logger.info(
        "agent upload deletion: workspace=%s deleted=%d failed=%d freed_bytes=%d",
        workspace,
        len(result["deleted"]),
        len(result["failed"]),
        result["freed_bytes"],
    )
    return _result(_render_deletion(result))
