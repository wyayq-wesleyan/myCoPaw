# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=line-too-long,too-many-return-statements
from __future__ import annotations

import mimetypes
import os
import shutil
import time
import unicodedata
from pathlib import Path

from agentscope.message import AudioBlock, ImageBlock, TextBlock, VideoBlock
from agentscope.tool import ToolResponse

from qwenpaw.app.agent_context import get_current_agent_id
from qwenpaw.config.utils import load_config
from qwenpaw.constant import WORKING_DIR

from ..schema import FileBlock
from .file_io import _resolve_file_path


def _int_env(name: str, default: int, *, min_value: int = 0) -> int:
    try:
        value = int(os.environ.get(name, str(default)).strip())
    except (AttributeError, TypeError, ValueError):
        return default
    return max(min_value, value)


def _safe_filename(name: str) -> str:
    cleaned = []
    for char in name:
        if char.isalnum() or char in {".", "-", "_"}:
            cleaned.append(char)
        else:
            cleaned.append("_")
    collapsed = "".join(cleaned).strip("._")
    return collapsed or "download"


def _workspace_media_dir() -> Path:
    try:
        agent_id = get_current_agent_id()
        config = load_config()
        agent_ref = config.agents.profiles.get(agent_id)
        if agent_ref:
            return Path(agent_ref.workspace_dir).expanduser().resolve() / "media"
    except Exception:
        pass
    return WORKING_DIR / "media"


def _cleanup_generated_files(media_dir: Path) -> None:
    ttl_hours = _int_env("COPAW_GENERATED_FILE_TTL_HOURS", 24)
    max_files = _int_env("COPAW_GENERATED_FILE_MAX_FILES", 200)
    max_total_mb = _int_env("COPAW_GENERATED_FILE_MAX_TOTAL_MB", 1024)

    now = time.time()
    files = []
    total_bytes = 0
    for path in media_dir.glob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append((path, stat.st_mtime, stat.st_size))
        total_bytes += stat.st_size

    ttl_seconds = ttl_hours * 3600
    for path, mtime, _size in files:
        if ttl_seconds and now - mtime > ttl_seconds:
            try:
                path.unlink()
            except OSError:
                pass

    files = []
    total_bytes = 0
    for path in media_dir.glob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append((path, stat.st_mtime, stat.st_size))
        total_bytes += stat.st_size

    files.sort(key=lambda item: item[1])
    max_total_bytes = max_total_mb * 1024 * 1024
    while len(files) > max_files or total_bytes > max_total_bytes:
        path, _mtime, size = files.pop(0)
        try:
            path.unlink()
            total_bytes -= size
        except OSError:
            pass


def _prepare_downloadable_copy(file_path: str) -> Path:
    source = Path(file_path).expanduser().resolve()
    media_dir = _workspace_media_dir()
    media_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_generated_files(media_dir)

    safe_name = _safe_filename(source.name)
    destination = media_dir / safe_name
    if destination.exists():
        stem = destination.stem
        suffix = destination.suffix
        destination = media_dir / f"{stem}_{int(time.time())}{suffix}"

    shutil.copy2(source, destination)
    return destination


def _auto_as_type(mt: str) -> str:
    if mt.startswith("image/"):
        return "image"
    if mt.startswith("audio/"):
        return "audio"
    if mt.startswith("video/"):
        return "video"
    return "file"


async def send_file_to_user(
    file_path: str,
) -> ToolResponse:
    """Send a file to the user via a previewable workspace URL."""

    file_path = os.path.expanduser(unicodedata.normalize("NFC", file_path))
    file_path = _resolve_file_path(file_path)

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

    mime_type, _ = mimetypes.guess_type(file_path)
    if mime_type is None:
        mime_type = "application/octet-stream"
    as_type = _auto_as_type(mime_type)

    try:
        downloadable_path = _prepare_downloadable_copy(file_path)
        source = {"type": "url", "url": str(downloadable_path)}

        if as_type == "image":
            return ToolResponse(
                content=[
                    ImageBlock(type="image", source=source),
                    TextBlock(type="text", text="File sent successfully."),
                ],
            )
        if as_type == "audio":
            return ToolResponse(
                content=[
                    AudioBlock(type="audio", source=source),
                    TextBlock(type="text", text="File sent successfully."),
                ],
            )
        if as_type == "video":
            return ToolResponse(
                content=[
                    VideoBlock(type="video", source=source),
                    TextBlock(type="text", text="File sent successfully."),
                ],
            )

        return ToolResponse(
            content=[
                FileBlock(
                    type="file",
                    source=source,
                    filename=downloadable_path.name,
                ),
                TextBlock(type="text", text="File sent successfully."),
            ],
        )
    except Exception as exc:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: Send file failed due to \n{exc}",
                ),
            ],
        )
