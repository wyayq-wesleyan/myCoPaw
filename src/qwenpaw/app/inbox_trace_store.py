# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..constant import WORKING_DIR
from ..utils.io_utils import (
    read_json,
    run_sync_io,
    unlink_async,
    write_json_atomic,
)

_TRACE_DIR = WORKING_DIR / "inbox_traces"
_LOCK = asyncio.Lock()


def _owner_slug(owner: str | None) -> str:
    """Filesystem-safe directory name for an account.

    Traces used to share one flat directory, so any account that learned a run
    id could read another's trace. Each account now writes under its own
    subtree; the name is hashed for non-ASCII accounts so CJK usernames cannot
    produce an unusable path.
    """
    import hashlib

    name = (owner or "").strip()
    if not name:
        return "_unowned"
    if name.isascii() and name.isalnum():
        return name
    return "u_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]


def _trace_path(run_id: str, owner: str | None = None) -> Path:
    """Path for one trace file.

    ``owner`` selects the account subtree. Callers that only have a run id (the
    read path) resolve the file by searching the subtrees, so the layout change
    is not visible to them.
    """
    safe_run = Path(run_id).name
    if owner is None:
        return _TRACE_DIR / f"{safe_run}.json"
    return _TRACE_DIR / _owner_slug(owner) / f"{safe_run}.json"


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if hasattr(value, "model_dump"):
        return _to_jsonable(value.model_dump(mode="json"))
    if hasattr(value, "dict"):
        return _to_jsonable(value.dict())
    return {"repr": repr(value)}


def _locate_trace(run_id: str) -> "Path | None":
    """Find an existing trace file, whatever layout wrote it.

    New traces live under an account subtree; traces written before the split
    sit directly in _TRACE_DIR. Both are searched so an in-flight run is not
    lost across the upgrade.
    """
    safe_run = Path(run_id).name
    candidates = [_TRACE_DIR / f"{safe_run}.json"]
    try:
        candidates.extend(sorted(_TRACE_DIR.glob(f"*/{safe_run}.json")))
    except OSError:
        pass
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _read_trace(run_id: str) -> dict[str, Any]:
    path = _locate_trace(run_id)
    if path is None:
        return {
            "run_id": run_id,
            "created_at": time.time(),
            "completed_at": None,
            "status": "running",
            "meta": {},
            "events": [],
        }
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError("invalid trace file")
    data.setdefault("events", [])
    return data


def _write_trace(run_id: str, payload: dict[str, Any]) -> None:
    existing = _locate_trace(run_id)
    path = existing if existing is not None else _trace_path(
        run_id,
        payload.get("owner"),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        path,
        payload,
        sort_keys=True,
    )


async def create_trace(
    run_id: str,
    *,
    meta: dict[str, Any] | None = None,
    owner: str | None = None,
) -> None:
    """Create a trace file.

    ``owner`` records the account the run belongs to. Traces are stored in one
    shared directory and read by run id, so without it any authenticated
    account that learned a run id could read another account's full trace.
    """
    async with _LOCK:
        payload = {
            "run_id": run_id,
            "created_at": time.time(),
            "completed_at": None,
            "status": "running",
            "meta": _to_jsonable(meta or {}),
            "owner": owner or "",
            "events": [],
        }
        await run_sync_io(_write_trace, run_id, payload)


async def append_trace_events(
    run_id: str,
    events: list[dict[str, Any]],
) -> None:
    if not events:
        return

    normalized_events: list[dict[str, Any]] = []
    for item in events:
        if not isinstance(item, dict):
            continue
        normalized_events.append(
            {
                "at": item.get("at")
                if item.get("at") is not None
                else time.time(),
                "event": _to_jsonable(item.get("event")),
            },
        )
    if not normalized_events:
        return

    async with _LOCK:
        payload = await run_sync_io(_read_trace, run_id)
        existing_events = payload.get("events", [])
        existing_events.extend(normalized_events)
        payload["events"] = existing_events
        await run_sync_io(_write_trace, run_id, payload)


def flatten_session_messages(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    messages: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, dict):
            messages.append(item)
            continue
        if isinstance(item, list) and item and isinstance(item[0], dict):
            messages.append(item[0])
    return messages


def parse_session_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).timestamp()
        except ValueError:
            continue
    return None


async def read_session_messages(
    *,
    runner: Any,
    session_id: str,
    user_id: str,
    channel: str,
) -> list[dict[str, Any]]:
    session = getattr(runner, "session", None)
    if session is None:
        return []
    try:
        state = await session.get_session_state_dict(
            session_id,
            user_id,
            channel,
            allow_not_exist=True,
        )
    except Exception:  # pylint: disable=broad-except
        return []
    agent_state = state.get("agent", {}).get("state", {})
    return flatten_session_messages(agent_state.get("context"))


async def append_trace_from_session_delta(
    *,
    run_id: str,
    runner: Any,
    session_id: str,
    user_id: str,
    channel: str,
    baseline_count: int,
) -> list[dict[str, Any]]:
    messages = await read_session_messages(
        runner=runner,
        session_id=session_id,
        user_id=user_id,
        channel=channel,
    )
    baseline_count = max(baseline_count, 0)
    delta = messages[baseline_count:]
    await append_trace_events(
        run_id,
        [
            {
                "at": parse_session_timestamp(
                    msg.get("created_at") or msg.get("timestamp"),
                ),
                "event": msg,
            }
            for msg in delta
        ],
    )
    return delta


async def finalize_trace(
    run_id: str,
    *,
    status: str,
    error: str | None = None,
) -> None:
    async with _LOCK:
        payload = await run_sync_io(_read_trace, run_id)
        payload["status"] = status
        payload["completed_at"] = time.time()
        if error is not None:
            payload["error"] = error
        await run_sync_io(_write_trace, run_id, payload)


async def get_trace(
    run_id: str,
    *,
    requester: str | None = None,
    is_admin: bool = False,
) -> dict[str, Any] | None:
    """Return one trace.

    When *requester* is given the trace is returned only if it belongs to that
    account (administrators may read any). A trace with no recorded owner is
    treated as admin-only rather than public, so legacy files cannot leak.
    """
    if _locate_trace(run_id) is None:
        return None
    async with _LOCK:
        payload = await run_sync_io(_read_trace, run_id)
    if requester is not None and not is_admin:
        owner = payload.get("owner") or ""
        if owner != requester:
            return None
    return payload


async def delete_trace(run_id: str) -> bool:
    if not run_id:
        return False
    async with _LOCK:
        path = _locate_trace(run_id)
        if path is None:
            return False
        await unlink_async(path)
    return True
