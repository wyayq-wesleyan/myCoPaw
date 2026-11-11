# -*- coding: utf-8 -*-
"""Structured audit for ordinary-user sandbox executions.

The sandbox protects data at the kernel boundary; this module makes the
boundary observable.  Each command is appended to a root-only JSONL file under
the working directory, outside every sandbox mount.  A failure to write the
audit entry is logged but never changes the command's result.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import ExecutionResult

logger = logging.getLogger(__name__)

_AUDIT_LOCK = threading.Lock()
_MAX_COMMAND_CHARS = 16 * 1024
_MAX_FIELD_CHARS = 4 * 1024
_MAX_FILE_BYTES = 20 * 1024 * 1024


def audit_path() -> Path:
    """Resolve the audit file without importing the app config at module load."""
    configured = os.environ.get("QWENPAW_LOG_DIR", "").strip()
    if configured:
        return Path(configured).expanduser() / "sandbox-audit.jsonl"
    from ..constant import WORKING_DIR

    return Path(WORKING_DIR) / "logs" / "sandbox-audit.jsonl"


def _truncate(value: str, limit: int = _MAX_FIELD_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n...[truncated {len(value) - limit} chars]"


def _rotate(path: Path) -> None:
    if not path.is_file() or path.stat().st_size < _MAX_FILE_BYTES:
        return
    backup = path.with_name(path.name + ".1")
    try:
        os.replace(path, backup)
    except OSError:
        logger.warning("sandbox audit: could not rotate %s", path, exc_info=True)


def record_sandbox_execution(
    username: str,
    uid: int,
    cwd: str,
    command: str,
    result: ExecutionResult,
) -> None:
    """Append one audit record. Never raises into the tool path."""
    try:
        path = audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "user_sandbox_exec",
            "user": username,
            "uid": uid,
            "cwd": str(cwd),
            "command": _truncate(command),
            "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "timed_out": bool(result.timed_out),
            "violation": _truncate(result.sandbox_violation or ""),
            "stdout_bytes": len(result.stdout.encode("utf-8", errors="replace")),
            "stderr_bytes": len(result.stderr.encode("utf-8", errors="replace")),
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _AUDIT_LOCK:
            _rotate(path)
            fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    except Exception:  # noqa: BLE001 - audit must not break execution
        logger.warning("sandbox audit: failed to record execution", exc_info=True)
