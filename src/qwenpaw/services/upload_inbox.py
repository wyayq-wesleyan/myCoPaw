# -*- coding: utf-8 -*-
"""Inbox uploads: one fixed destination, optional server-side extraction.

Every file uploaded from the Files page lands in ``<workspace>/upload``
(created on demand).  Archives that the image can unpack with the standard
library (zip / tar / tar.gz / tgz / gz) are extracted by a background job;
the archive itself is kept, so a failed or unsupported extraction never loses
the user's data.

Extraction is deliberately conservative: this is the one place where a
user-supplied file can create an arbitrary number of paths, so path traversal,
symlinks, device nodes, zip bombs and unbounded trees are all rejected before
anything is written outside the staging directory.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tarfile
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import UploadFile

logger = logging.getLogger(__name__)

INBOX_DIR_NAME = "upload"
JOB_DIR_NAME = ".jobs"

_DEFAULT_MAX_FILE_MB = 10240  # 10 GiB
_DEFAULT_MAX_EXTRACT_GB = 50
_MAX_EXTRACT_FILES = 200_000
_MAX_EXTRACT_DEPTH = 32
_MAX_COMPRESSION_RATIO = 200
_MAX_CONCURRENT_EXTRACTIONS = 2
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_READ_CHUNK = 1024 * 1024

_extraction_slots: asyncio.Semaphore | None = None
_extraction_slots_loop: asyncio.AbstractEventLoop | None = None


def _extraction_semaphore() -> asyncio.Semaphore:
    """Return a semaphore bound to the running loop (safe across test loops)."""
    global _extraction_slots, _extraction_slots_loop
    loop = asyncio.get_running_loop()
    if _extraction_slots is None or _extraction_slots_loop is not loop:
        _extraction_slots = asyncio.Semaphore(_MAX_CONCURRENT_EXTRACTIONS)
        _extraction_slots_loop = loop
    return _extraction_slots


class InboxUploadError(Exception):
    """Raised when an upload itself is rejected (size, name, IO)."""


class InboxConflictError(InboxUploadError):
    """Raised when an entry operation collides with an existing path."""


class ArchiveError(Exception):
    """Raised when an archive is unsupported, unsafe, or exceeds a limit."""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric %s=%r", name, raw)
        return default
    return value if value > 0 else default


def max_upload_bytes() -> int:
    """Per-file upload ceiling (default 10 GiB)."""
    return _env_int("QWENPAW_INBOX_MAX_FILE_MB", _DEFAULT_MAX_FILE_MB) * (
        1024 * 1024
    )


def max_extract_bytes() -> int:
    """Total bytes one archive may expand to (default 50 GiB)."""
    return _env_int("QWENPAW_INBOX_MAX_EXTRACT_GB", _DEFAULT_MAX_EXTRACT_GB) * (
        1024 * 1024 * 1024
    )


def inbox_dir(workspace_dir: Path) -> Path:
    """Return (and create) the fixed upload directory for one workspace."""
    directory = Path(workspace_dir) / INBOX_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def job_dir(workspace_dir: Path) -> Path:
    """Return (and create) the hidden job-status directory."""
    directory = inbox_dir(workspace_dir) / JOB_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


_MAX_FILENAME_BYTES = 200
_MAX_SUFFIX_BYTES = 20


def _truncate_utf8(value: str, limit: int) -> str:
    """Trim *value* to at most *limit* UTF-8 bytes without splitting a char."""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    truncated = encoded[:limit]
    while truncated:
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    return ""


def safe_filename(name: str | None) -> str:
    """Reduce an upload to one safe path component.

    Browsers normally send a basename, but a crafted multipart body can send
    ``../x`` or an absolute path; only the last component is ever used.
    """
    raw = (name or "").replace("\\", "/").strip()
    base = raw.rsplit("/", 1)[-1]
    base = base.replace("\x00", "").strip()
    if base in ("", ".", ".."):
        raise InboxUploadError("文件名无效")
    if base.startswith("."):
        base = f"file{base}"
    # ext4/xfs allow 255 bytes per component; Chinese names hit the limit at
    # ~85 characters, so truncate by bytes rather than characters.
    if len(base.encode("utf-8")) > _MAX_FILENAME_BYTES:
        stem, dot, suffix = base.rpartition(".")
        suffix_bytes = len(suffix.encode("utf-8"))
        if dot and suffix_bytes <= _MAX_SUFFIX_BYTES:
            suffix_part = f".{suffix}"
            stem = _truncate_utf8(
                stem,
                _MAX_FILENAME_BYTES - len(suffix_part.encode("utf-8")),
            )
            base = f"{stem or 'file'}{suffix_part}"
        else:
            base = _truncate_utf8(base, _MAX_FILENAME_BYTES) or "file"
    return base


def entry_name(name: str | None) -> str:
    """Validate an explicit rename target (one path component, no rewriting)."""
    raw = (name or "").strip()
    if not raw or raw in (".", "..") or "/" in raw or "\\" in raw:
        raise InboxUploadError("文件名无效")
    if "\x00" in raw:
        raise InboxUploadError("文件名无效")
    if raw.startswith("."):
        # Hidden names are invisible in the Files tree and `.jobs` is the
        # extraction-job namespace; neither should be reachable by rename.
        raise InboxUploadError("文件名不能以 . 开头")
    if len(raw.encode("utf-8")) > _MAX_FILENAME_BYTES:
        raise InboxUploadError(
            f"文件名过长（最多 {_MAX_FILENAME_BYTES} 字节）",
        )
    return raw


def inbox_entry_path(workspace_dir: Path, api_path: str) -> Path:
    """Resolve an API path to an entry strictly inside ``upload/``.

    The Files tree uses workspace-relative paths (``upload/dir/a.txt``). Only
    that prefix is accepted; ``..``, absolute paths, the inbox root itself and
    the internal ``.jobs`` directory are all rejected.
    """
    root = inbox_dir(workspace_dir).resolve()
    raw = (api_path or "").strip().replace("\\", "/")
    if "\x00" in raw:
        raise InboxUploadError("路径无效")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or parts[0] != INBOX_DIR_NAME:
        raise InboxUploadError("只能管理 upload/ 下的文件")
    if any(part == ".." for part in parts):
        raise InboxUploadError("路径不能包含 ..")
    if JOB_DIR_NAME in parts[1:]:
        raise InboxUploadError("不能操作内部任务目录")
    if len(parts) == 1:
        raise InboxUploadError("不能操作 upload 目录本身")
    candidate = root.joinpath(*parts[1:])
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise InboxUploadError(f"无法解析路径：{exc}") from exc
    if resolved == root or not resolved.is_relative_to(root):
        raise InboxUploadError("路径不在 upload 目录内")
    return candidate


def rename_inbox_entry(
    workspace_dir: Path,
    api_path: str,
    new_name: str,
) -> str:
    """Rename one entry under ``upload/``; returns the new workspace path."""
    source = inbox_entry_path(workspace_dir, api_path)
    if not os.path.lexists(source):
        raise FileNotFoundError(api_path)
    name = entry_name(new_name)
    target = source.parent / name
    root = Path(workspace_dir).resolve()
    if target == source:
        return target.relative_to(root).as_posix()
    if os.path.lexists(target):
        raise InboxConflictError(f"同名文件已存在：{name}")
    os.rename(source, target)
    return target.relative_to(root).as_posix()


def delete_inbox_entry(
    workspace_dir: Path,
    api_path: str,
    *,
    recursive: bool = False,
) -> None:
    """Delete one entry under ``upload/`` (directories need *recursive*)."""
    source = inbox_entry_path(workspace_dir, api_path)
    if not os.path.lexists(source):
        raise FileNotFoundError(api_path)
    if source.is_symlink():
        # Never follow the link: remove the link itself.
        source.unlink()
        return
    if source.is_dir():
        if not recursive:
            try:
                next(source.iterdir())
            except StopIteration:
                pass
            else:
                raise InboxConflictError("目录非空，需要确认递归删除")
        # ``rmtree`` does not follow symlinks encountered inside the tree.
        shutil.rmtree(source)
        return
    source.unlink()


def _allocated_bytes(info: os.stat_result) -> int:
    """Return bytes actually allocated on disk (``st_size`` as fallback)."""
    blocks = getattr(info, "st_blocks", 0)
    return blocks * 512 if blocks else info.st_size


def entry_disk_usage(path: Path) -> int:
    """Allocated bytes for one file or directory tree, without following links."""
    try:
        root_stat = path.lstat()
    except OSError:
        return 0
    total = _allocated_bytes(root_stat)
    if not stat.S_ISDIR(root_stat.st_mode):
        return total
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as scanner:
                for entry in scanner:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += _allocated_bytes(info)
                    if stat.S_ISDIR(info.st_mode):
                        stack.append(Path(entry.path))
        except OSError:
            continue
    return total


def _dedupe_entry_paths(
    workspace_dir: Path,
    api_paths: list[str],
) -> list[tuple[str, Path]]:
    """Validate paths, drop duplicates, and keep only the outermost entries."""
    root = inbox_dir(workspace_dir).resolve()
    unique: dict[str, Path] = {}
    for api_path in api_paths:
        source = inbox_entry_path(workspace_dir, api_path)
        relative = source.relative_to(root).as_posix()
        unique.setdefault(relative, source)
    ordered = sorted(unique.items(), key=lambda item: item[0].count("/"))
    kept: list[tuple[str, Path]] = []
    for relative, source in ordered:
        if any(
            relative == parent or relative.startswith(f"{parent}/")
            for parent, _ in kept
        ):
            continue
        kept.append((relative, source))
    return kept


def describe_inbox_entries(
    workspace_dir: Path,
    api_paths: list[str],
) -> list[dict[str, Any]]:
    """Return deletion candidates with kind, allocated size and existence."""
    rows: list[dict[str, Any]] = []
    for relative, source in _dedupe_entry_paths(workspace_dir, api_paths):
        try:
            is_directory = source.is_dir() and not source.is_symlink()
        except OSError:
            is_directory = False
        rows.append(
            {
                "path": f"{INBOX_DIR_NAME}/{relative}",
                "kind": "directory" if is_directory else "file",
                "bytes": entry_disk_usage(source),
                "exists": os.path.lexists(source),
            },
        )
    return rows


def delete_inbox_entries(
    workspace_dir: Path,
    api_paths: list[str],
    *,
    recursive: bool = False,
) -> dict[str, Any]:
    """Delete several entries and verify each path is gone afterwards.

    Returns ``{"deleted": [...], "failed": [...], "freed_bytes": N}`` where
    ``freed_bytes`` is the allocated size measured before unlink/rmtree.
    """
    kept = _dedupe_entry_paths(workspace_dir, api_paths)
    if not kept:
        raise InboxUploadError("没有要删除的文件")
    deleted: list[str] = []
    failed: list[dict[str, str]] = []
    freed_bytes = 0
    for relative, source in kept:
        api_path = f"{INBOX_DIR_NAME}/{relative}"
        if not os.path.lexists(source):
            failed.append({"path": api_path, "error": "文件不存在"})
            continue
        size = entry_disk_usage(source)
        try:
            delete_inbox_entry(workspace_dir, api_path, recursive=recursive)
        except (InboxUploadError, OSError) as exc:
            failed.append({"path": api_path, "error": str(exc)})
            continue
        # The whole point is reclaiming disk: never claim success while the
        # path is still there.
        if os.path.lexists(source):
            failed.append({"path": api_path, "error": "删除后文件仍存在"})
            continue
        freed_bytes += size
        deleted.append(api_path)
    return {
        "deleted": deleted,
        "failed": failed,
        "freed_bytes": freed_bytes,
    }


def expand_upload_patterns(
    workspace_dir: Path,
    patterns: list[str],
    *,
    limit: int = 5000,
) -> list[str]:
    """Expand glob rules relative to ``upload/`` into API paths.

    ``*`` and ``**`` are supported. Rules may not be absolute or contain
    ``..``; every match is re-checked after resolving symlinks.
    """
    root = inbox_dir(workspace_dir).resolve()
    results: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        raw = (pattern or "").strip().replace("\\", "/")
        if not raw or "\x00" in raw or raw.startswith("/"):
            raise InboxUploadError(f"通配规则无效：{pattern!r}")
        parts = [part for part in raw.split("/") if part not in ("", ".")]
        if not parts or any(part == ".." for part in parts):
            raise InboxUploadError(f"通配规则无效：{pattern!r}")
        if JOB_DIR_NAME in parts:
            raise InboxUploadError(f"通配规则无效：{pattern!r}")
        if parts[0] == INBOX_DIR_NAME:
            parts = parts[1:]
        if not parts:
            raise InboxUploadError("通配规则不能匹配 upload 目录本身")
        for hit in root.glob("/".join(parts)):
            try:
                relative = hit.relative_to(root)
                if JOB_DIR_NAME in relative.parts:
                    continue
                if not hit.resolve().is_relative_to(root):
                    continue
            except (OSError, ValueError):
                continue
            api_path = f"{INBOX_DIR_NAME}/{relative.as_posix()}"
            if api_path in seen:
                continue
            seen.add(api_path)
            results.append(api_path)
            if len(results) > limit:
                raise InboxUploadError(
                    f"匹配项超过 {limit} 个，请缩小删除规则",
                )
    return results


def _delete_source_archive(archive: Path, workspace_dir: Path) -> bool:
    """Delete an extracted archive, but only inside this workspace's inbox."""
    try:
        root = inbox_dir(workspace_dir).resolve()
        resolved = archive.resolve()
        if resolved == root or not resolved.is_relative_to(root):
            logger.warning(
                "upload inbox: refusing to delete archive outside inbox: %s",
                archive,
            )
            return False
        resolved.unlink(missing_ok=True)
        return not resolved.exists()
    except OSError:
        logger.warning(
            "upload inbox: could not delete archive %s",
            archive,
            exc_info=True,
        )
        return False


def _split_name(name: str) -> tuple[str, str]:
    """Split ``x.tar.gz`` into (``x``, ``.tar.gz``); other names use suffix."""
    lower = name.lower()
    for compound in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz"):
        if lower.endswith(compound):
            return name[: -len(compound)], name[-len(compound) :]
    suffix = Path(name).suffix
    if suffix:
        return name[: -len(suffix)], suffix
    return name, ""


def unique_target(directory: Path, filename: str) -> Path:
    """Return a non-existing path, suffixing `` (2)``, `` (3)`` … on conflict."""
    directory.mkdir(parents=True, exist_ok=True)
    stem, suffix = _split_name(filename)
    for index in range(1, 10_001):
        candidate = directory / (
            filename if index == 1 else f"{stem} ({index}){suffix}"
        )
        if not candidate.exists():
            return candidate
    raise InboxUploadError("同名文件过多，无法分配文件名")


def _reserve_target(directory: Path, filename: str) -> Path:
    """Atomically reserve a conflict-free target (creates an empty file)."""
    directory.mkdir(parents=True, exist_ok=True)
    stem, suffix = _split_name(filename)
    for index in range(1, 10_001):
        candidate = directory / (
            filename if index == 1 else f"{stem} ({index}){suffix}"
        )
        try:
            descriptor = os.open(
                candidate,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            continue
        os.close(descriptor)
        return candidate
    raise InboxUploadError("同名文件过多，无法分配文件名")


def reserve_target(directory: Path, filename: str) -> Path:
    """Reserve a conflict-free target path, creating an empty placeholder."""
    return _reserve_target(directory, filename)


def save_upload(
    upload: UploadFile,
    target: Path,
    *,
    limit_bytes: int | None = None,
) -> tuple[int, str]:
    """Stream one upload into *target*, returning (size, sha256).

    The bytes go to a sibling temp file first; a failure or a size overrun
    leaves no partial file behind.  ``target`` is an already reserved empty
    file, so the final replace is atomic.
    """
    limit = max_upload_bytes() if limit_bytes is None else limit_bytes
    temporary = target.with_name(
        f".{target.name}.{uuid.uuid4().hex[:8]}.upload.tmp",
    )
    digest = hashlib.sha256()
    size = 0
    try:
        upload.file.seek(0)
        with temporary.open("wb") as handle:
            while True:
                chunk = upload.file.read(_READ_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise InboxUploadError(
                        f"文件超过上限（{limit // (1024 * 1024)} MB）",
                    )
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise
    return size, digest.hexdigest()


# ---------------------------------------------------------------------------
# Archive detection and extraction
# ---------------------------------------------------------------------------


def detect_archive(path: Path) -> str | None:
    """Return ``zip`` / ``tar`` / ``gz`` when the file is a supported archive."""
    try:
        if zipfile.is_zipfile(path):
            return "zip"
        if tarfile.is_tarfile(path):
            return "tar"
        with open(path, "rb") as handle:
            if handle.read(2) == b"\x1f\x8b":
                return "gz"
    except OSError:
        return None
    return None


def _member_parts(name: str) -> list[str]:
    """Normalize an archive member name and reject traversal."""
    normalized = name.replace("\\", "/").strip()
    if not normalized or normalized.startswith("/") or re.match(
        r"^[A-Za-z]:",
        normalized,
    ):
        raise ArchiveError(f"压缩包包含非法路径：{name!r}")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ArchiveError(f"压缩包包含路径穿越：{name!r}")
    if len(parts) > _MAX_EXTRACT_DEPTH:
        raise ArchiveError(f"压缩包目录过深：{name!r}")
    return parts


def _within(base: Path, candidate: Path) -> Path:
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ArchiveError("压缩包条目逃出目标目录")
    return resolved


def _check_limits(
    *,
    file_count: int,
    total_bytes: int,
    archive_bytes: int,
    expanded_bytes: int = 0,
) -> None:
    if file_count > _MAX_EXTRACT_FILES:
        raise ArchiveError(
            f"压缩包文件数超过上限（{_MAX_EXTRACT_FILES}）",
        )
    if total_bytes > max_extract_bytes():
        raise ArchiveError(
            "压缩包解压后超过大小上限"
            f"（{max_extract_bytes() // (1024 ** 3)} GB）",
        )
    expanded = max(total_bytes, expanded_bytes)
    if (
        archive_bytes > 0
        and expanded > archive_bytes * _MAX_COMPRESSION_RATIO
        and expanded > 1024 * 1024
    ):
        raise ArchiveError("压缩比异常，疑似压缩炸弹")


def _extract_zip(
    archive: Path,
    staging: Path,
    archive_bytes: int,
) -> tuple[int, int]:
    file_count = 0
    total_bytes = 0
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        # The central directory is always fully parsed, so reject an oversized
        # index before doing any extraction work.
        if len(infos) > _MAX_EXTRACT_FILES:
            raise ArchiveError(
                f"压缩包文件数超过上限（{_MAX_EXTRACT_FILES}）",
            )
        for info in infos:
            parts = _member_parts(info.filename)
            if not parts or info.is_dir():
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                logger.warning(
                    "upload inbox: skipping symlink in zip: %s",
                    info.filename,
                )
                continue
            file_count += 1
            total_bytes += info.file_size
            if (
                info.file_size > 1024 * 1024
                and info.compress_size > 0
                and info.file_size / info.compress_size
                > _MAX_COMPRESSION_RATIO
            ):
                raise ArchiveError("压缩比异常，疑似压缩炸弹")
            _check_limits(
                file_count=file_count,
                total_bytes=total_bytes,
                archive_bytes=archive_bytes,
            )
            destination = _within(staging, staging.joinpath(*parts))
            destination.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target, _READ_CHUNK)
    return file_count, total_bytes


def _extract_tar(
    archive: Path,
    staging: Path,
    archive_bytes: int,
) -> tuple[int, int]:
    file_count = 0
    total_bytes = 0
    with tarfile.open(archive, "r:*") as bundle:
        # Stream members instead of ``getmembers()``: a malicious archive with
        # millions of headers must not be materialized in memory first.
        for member in bundle:
            if member.isdir():
                _member_parts(member.name)
                continue
            if member.issym() or member.islnk():
                logger.warning(
                    "upload inbox: skipping link in tar: %s",
                    member.name,
                )
                continue
            if member.ischr() or member.isblk() or member.isfifo() or member.isdev():
                logger.warning(
                    "upload inbox: skipping special file in tar: %s",
                    member.name,
                )
                continue
            parts = _member_parts(member.name)
            if not parts:
                continue
            file_count += 1
            total_bytes += member.size
            _check_limits(
                file_count=file_count,
                total_bytes=total_bytes,
                archive_bytes=archive_bytes,
            )
            source = bundle.extractfile(member)
            if source is None:
                continue
            destination = _within(staging, staging.joinpath(*parts))
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source, destination.open("wb") as target:
                shutil.copyfileobj(source, target, _READ_CHUNK)
    return file_count, total_bytes


def _extract_gz(
    archive: Path,
    staging: Path,
    archive_bytes: int,
) -> tuple[int, int]:
    stem, _ = _split_name(archive.name)
    destination = _within(staging, staging / (stem or "extracted"))
    total_bytes = 0
    with gzip.open(archive, "rb") as source, destination.open("wb") as target:
        while True:
            chunk = source.read(_READ_CHUNK)
            if not chunk:
                break
            total_bytes += len(chunk)
            _check_limits(
                file_count=1,
                total_bytes=total_bytes,
                archive_bytes=archive_bytes,
            )
            target.write(chunk)
    return 1, total_bytes


def _flatten_single_root(staging: Path, target: Path) -> None:
    """Move the extracted tree into *target*, dropping one redundant root.

    ``data.zip`` containing only ``data/...`` should land as ``upload/data/...``
    rather than ``upload/data/data/...``.
    """
    entries = [entry for entry in staging.iterdir()]
    if len(entries) == 1 and entries[0].is_dir():
        os.replace(entries[0], target)
        staging.rmdir()
        return
    os.replace(staging, target)


def extract_archive(archive: Path, target: Path) -> dict[str, Any]:
    """Extract *archive* into *target* with safety limits.

    Extraction happens in a sibling staging directory and is only moved into
    place after it succeeds, so an interrupted or rejected archive never
    leaves a half-populated target.
    """
    kind = detect_archive(archive)
    if kind is None:
        raise ArchiveError("不支持的压缩格式（仅支持 zip / tar / tar.gz / gz）")
    archive_bytes = archive.stat().st_size
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{target.name}.extract-",
            dir=str(target.parent),
        ),
    )
    try:
        if kind == "zip":
            file_count, total_bytes = _extract_zip(
                archive,
                staging,
                archive_bytes,
            )
        elif kind == "tar":
            file_count, total_bytes = _extract_tar(
                archive,
                staging,
                archive_bytes,
            )
        else:
            file_count, total_bytes = _extract_gz(
                archive,
                staging,
                archive_bytes,
            )
        _check_limits(
            file_count=file_count,
            total_bytes=total_bytes,
            archive_bytes=archive_bytes,
        )
        final_target = unique_target(target.parent, target.name)
        _flatten_single_root(staging, final_target)
        return {
            "kind": kind,
            "files": file_count,
            "bytes": total_bytes,
            "target": final_target,
        }
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# Extraction jobs
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_path(workspace_dir: Path, job_id: str) -> Path:
    if not _JOB_ID_RE.fullmatch(job_id):
        raise InboxUploadError("任务编号无效")
    return job_dir(workspace_dir) / f"{job_id}.json"


def write_job(workspace_dir: Path, job: dict[str, Any]) -> None:
    path = _job_path(workspace_dir, str(job["id"]))
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(job, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def read_job(workspace_dir: Path, job_id: str) -> dict[str, Any] | None:
    path = _job_path(workspace_dir, job_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def create_extract_job(
    workspace_dir: Path,
    archive: Path,
) -> dict[str, Any]:
    """Create a queued extraction job for *archive*."""
    inbox = inbox_dir(workspace_dir)
    stem, _ = _split_name(archive.name)
    job = {
        "id": uuid.uuid4().hex,
        "name": archive.name,
        "archive": str(archive),
        "target": str(unique_target(inbox, stem or "extracted")),
        "status": "queued",
        "files": 0,
        "bytes": 0,
        "archive_deleted": False,
        "error": "",
        "created_at": _now(),
        "updated_at": _now(),
    }
    write_job(workspace_dir, job)
    return job


def run_extract_job(workspace_dir: Path, job_id: str) -> None:
    """Blocking worker: extract one job and persist its final status."""
    job = read_job(workspace_dir, job_id)
    if job is None:
        return
    job.update(status="extracting", updated_at=_now())
    write_job(workspace_dir, job)
    target = Path(job["target"])
    try:
        result = extract_archive(Path(job["archive"]), target)
        # The archive has served its purpose; keep it only when extraction
        # failed so the user can retry or download it.
        archive_deleted = _delete_source_archive(
            Path(job["archive"]),
            workspace_dir,
        )
        job.update(
            status="ready",
            files=result["files"],
            bytes=result["bytes"],
            target=str(result["target"]),
            archive_deleted=archive_deleted,
            error="",
            updated_at=_now(),
        )
    except ArchiveError as exc:
        shutil.rmtree(target, ignore_errors=True)
        job.update(
            status="failed",
            error=str(exc),
            updated_at=_now(),
        )
    except Exception as exc:  # noqa: BLE001 - a bad archive must not kill us
        logger.exception("upload inbox: extraction failed for %s", job_id)
        shutil.rmtree(target, ignore_errors=True)
        job.update(
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            updated_at=_now(),
        )
    write_job(workspace_dir, job)


def start_extract_job(workspace_dir: Path, job_id: str) -> None:
    """Run an extraction job in a worker thread without blocking the loop."""

    async def _run() -> None:
        try:
            # Bound concurrent disk-heavy extractions; queued jobs keep their
            # "queued" status until a slot is free.
            async with _extraction_semaphore():
                await asyncio.to_thread(run_extract_job, workspace_dir, job_id)
        except Exception:  # noqa: BLE001 - logged by run_extract_job
            logger.exception("upload inbox: job %s crashed", job_id)

    asyncio.create_task(_run())
