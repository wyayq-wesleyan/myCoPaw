# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=line-too-long,too-many-return-statements
import os
import mimetypes
import re
import shutil
import time
import unicodedata
import uuid
from pathlib import Path

from agentscope.tool import ToolResponse
from agentscope.message import (
    TextBlock,
    ImageBlock,
    AudioBlock,
    VideoBlock,
)

from ..schema import FileBlock
from ...config.context import get_current_workspace_dir
from ...constant import WORKING_DIR


def _auto_as_type(mt: str) -> str:
    if mt.startswith("image/"):
        return "image"
    if mt.startswith("audio/"):
        return "audio"
    if mt.startswith("video/"):
        return "video"
    return "file"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _safe_filename(name: str) -> str:
    base = Path(name).name if name else "file"
    safe = re.sub(r"[^\w.\-]", "_", base)[:200]
    return safe or "file"


def _cleanup_generated_files(media_dir: Path) -> None:
    """Best-effort lifecycle cleanup for generated downloadable artifacts."""
    ttl_hours = max(1, _int_env("COPAW_GENERATED_FILE_TTL_HOURS", 72))
    max_files = max(10, _int_env("COPAW_GENERATED_FILE_MAX_FILES", 300))
    max_total_mb = max(50, _int_env("COPAW_GENERATED_FILE_MAX_TOTAL_MB", 1024))
    max_total_bytes = max_total_mb * 1024 * 1024

    files = [
        p
        for p in media_dir.glob("generated_*")
        if p.is_file() and not p.is_symlink()
    ]
    if not files:
        return

    now = time.time()
    ttl_seconds = ttl_hours * 3600

    # First pass: delete expired files.
    kept = []
    for path in files:
        try:
            if now - path.stat().st_mtime > ttl_seconds:
                path.unlink(missing_ok=True)
            else:
                kept.append(path)
        except OSError:
            # Ignore individual file failures to keep tool robust.
            continue

    # Second pass: enforce max file count and total size (oldest first).
    stats = []
    total_size = 0
    for path in kept:
        try:
            st = path.stat()
        except OSError:
            continue
        total_size += st.st_size
        stats.append((path, st.st_mtime, st.st_size))

    stats.sort(key=lambda x: x[1])  # oldest first
    while len(stats) > max_files or total_size > max_total_bytes:
        path, _, size = stats.pop(0)
        try:
            path.unlink(missing_ok=True)
            total_size -= size
        except OSError:
            continue


def _prepare_downloadable_copy(file_path: str) -> Path:
    """Copy a file into workspace/media so console UI can download it."""
    src = Path(file_path).expanduser().resolve()
    workspace_dir = get_current_workspace_dir() or WORKING_DIR
    media_dir = Path(workspace_dir).expanduser() / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    try:
        if src.parent.resolve() == media_dir.resolve():
            return src
    except OSError:
        pass

    stored_name = (
        f"generated_{int(time.time())}_{uuid.uuid4().hex[:8]}_"
        f"{_safe_filename(src.name)}"
    )
    dest = media_dir / stored_name
    shutil.copy2(src, dest)
    _cleanup_generated_files(media_dir)
    return dest


def _build_console_download_markdown(path: Path) -> str:
    """Build a markdown download link for the console UI."""
    agent_id = os.environ.get("COPAW_AGENT_ID", "default") or "default"
    url = f"/api/console/files/{agent_id}/{path.name}"
    return f"下载链接：[{path.name}]({url})"


async def send_file_to_user(
    file_path: str,
) -> ToolResponse:
    """Send a file to the user.

    Args:
        file_path (`str`):
            Path to the file to send.

    Returns:
        `ToolResponse`:
            The tool response containing the file or an error message.
    """

    # Normalize the path: expand ~ and fix Unicode normalization differences
    # (e.g. macOS stores filenames as NFD but paths from the LLM arrive as NFC,
    # causing os.path.exists to return False for files that do exist).
    file_path = os.path.expanduser(unicodedata.normalize("NFC", file_path))

    if not os.path.exists(file_path):
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: The file {file_path} does not exist.",
                ),
            ],
        )

    if not os.path.isfile(file_path):
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: The path {file_path} is not a file.",
                ),
            ],
        )

    # Detect MIME type
    mime_type, _ = mimetypes.guess_type(file_path)
    if mime_type is None:
        # Default to application/octet-stream for unknown types
        mime_type = "application/octet-stream"
    as_type = _auto_as_type(mime_type)

    try:
        # Copy into workspace/media so Console UI can download it via
        # /api/console/files/{agent_id}/{filename}.
        absolute_path = os.path.abspath(file_path)
        downloadable_path = _prepare_downloadable_copy(absolute_path)
        file_url = f"file://{downloadable_path}"
        source = {"type": "url", "url": file_url}
        display_name = os.path.basename(absolute_path)
        download_text = _build_console_download_markdown(downloadable_path)

        if as_type == "image":
            return ToolResponse(
                content=[
                    TextBlock(type="text", text=download_text),
                    ImageBlock(type="image", source=source),
                    FileBlock(
                        type="file",
                        source=source,
                        filename=display_name,
                    ),
                ],
            )
        if as_type == "audio":
            return ToolResponse(
                content=[
                    TextBlock(type="text", text=download_text),
                    AudioBlock(type="audio", source=source),
                    FileBlock(
                        type="file",
                        source=source,
                        filename=display_name,
                    ),
                ],
            )
        if as_type == "video":
            return ToolResponse(
                content=[
                    TextBlock(type="text", text=download_text),
                    VideoBlock(type="video", source=source),
                    FileBlock(
                        type="file",
                        source=source,
                        filename=display_name,
                    ),
                ],
            )

        return ToolResponse(
            content=[
                TextBlock(type="text", text=download_text),
                FileBlock(
                    type="file",
                    source=source,
                    filename=display_name,
                ),
            ],
        )

    except Exception as e:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: Send file failed due to \n{e}",
                ),
            ],
        )
