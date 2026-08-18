"""Persistent ownership records for user-created skills and tools.

Built-in resources are intentionally not recorded here: they are part of the
offline base installation and cannot be forwarded between accounts.  This
registry only tracks resources created after an account starts using the
system, which gives sharing and approval a stable identity independent of a
workspace directory or display name.
"""
from __future__ import annotations

import asyncio
import copy
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pathlib import Path
from typing import Any

from ..constant import WORKING_DIR
from ..utils.io_utils import read_json, run_sync_io, write_json_atomic

_REGISTRY_PATH = WORKING_DIR / "user_resources.json"
_RESOURCE_ROOT = WORKING_DIR / "user_resources"
_LOCK = asyncio.Lock()


def _default_registry() -> dict[str, list[dict[str, Any]]]:
    return {"resources": [], "share_requests": []}


def _load_registry() -> dict[str, list[dict[str, Any]]]:
    if not _REGISTRY_PATH.is_file():
        return _default_registry()
    try:
        payload = read_json(_REGISTRY_PATH)
    except (OSError, ValueError):
        return _default_registry()
    if not isinstance(payload, dict):
        return _default_registry()
    return {
        "resources": [
            item for item in payload.get("resources", []) if isinstance(item, dict)
        ],
        "share_requests": [
            item
            for item in payload.get("share_requests", [])
            if isinstance(item, dict)
        ],
    }


def _save_registry(payload: dict[str, list[dict[str, Any]]]) -> None:
    _RESOURCE_ROOT.mkdir(parents=True, exist_ok=True)
    write_json_atomic(_REGISTRY_PATH, payload, sort_keys=True)


def resource_storage_dir(resource_id: str) -> Path:
    """Return the canonical directory containing a custom skill's files."""
    return _RESOURCE_ROOT / "skills" / resource_id


def find_resource_sync(
    *, kind: str, name: str, username: str, admin: bool = False
) -> dict[str, Any] | None:
    """Synchronous lookup used by list serializers."""
    payload = _load_registry()
    for record in reversed(payload["resources"]):
        if record.get("kind") != kind or record.get("name") != name:
            continue
        recipients = record.get("recipients") or []
        if admin or username == record.get("creator_username") or username in recipients:
            return copy.deepcopy(record)
    return None


def list_resources_for_user_sync(
    *, kind: str, username: str, admin: bool = False
) -> list[dict[str, Any]]:
    payload = _load_registry()
    return [
        copy.deepcopy(record)
        for record in payload["resources"]
        if record.get("kind") == kind
        and (
            admin
            or username == record.get("creator_username")
            or username in (record.get("recipients") or [])
        )
    ]


def register_resource_sync(
    *, kind: str, name: str, creator_username: str, source_label: str = "本人创建"
) -> dict[str, Any]:
    """Register a resource from synchronous skill/tool service code."""
    if kind not in {"skill", "tool"}:
        raise ValueError("Unsupported resource type")
    record = {
        "resource_id": str(uuid.uuid4()),
        "kind": kind,
        "name": name,
        "creator_username": creator_username,
        "source_label": source_label,
        "is_builtin": False,
        "created_at": time.time(),
        "recipients": [creator_username],
    }
    payload = _load_registry()
    payload["resources"].append(record)
    _save_registry(payload)
    return copy.deepcopy(record)


async def register_resource(
    *,
    kind: str,
    name: str,
    creator_username: str,
    source_label: str = "本人创建",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if kind not in {"skill", "tool"}:
        raise ValueError("Unsupported resource type")
    record = {
        "resource_id": str(uuid.uuid4()),
        "kind": kind,
        "name": name,
        "creator_username": creator_username,
        "source_label": source_label,
        "is_builtin": False,
        "created_at": time.time(),
        "recipients": [creator_username],
        "metadata": copy.deepcopy(metadata or {}),
    }
    async with _LOCK:
        payload = await run_sync_io(_load_registry)
        payload["resources"].append(record)
        await run_sync_io(_save_registry, payload)
    return copy.deepcopy(record)


async def find_visible_resource(
    *,
    kind: str,
    name: str,
    username: str,
    admin: bool = False,
) -> dict[str, Any] | None:
    async with _LOCK:
        payload = await run_sync_io(_load_registry)
    for record in reversed(payload["resources"]):
        if record.get("kind") != kind or record.get("name") != name:
            continue
        recipients = record.get("recipients") or []
        if admin or username == record.get("creator_username") or username in recipients:
            return copy.deepcopy(record)
    return None


async def list_resources_for_user(
    *, kind: str, username: str, admin: bool = False
) -> list[dict[str, Any]]:
    async with _LOCK:
        payload = await run_sync_io(_load_registry)
    records: list[dict[str, Any]] = []
    for record in payload["resources"]:
        if record.get("kind") != kind:
            continue
        recipients = record.get("recipients") or []
        if admin or username == record.get("creator_username") or username in recipients:
            records.append(copy.deepcopy(record))
    return records


async def create_share_requests(
    *, resource_id: str, sender_username: str, recipient_usernames: list[str]
) -> list[dict[str, Any]]:
    """Create approval requests. Only the original creator can distribute."""
    async with _LOCK:
        payload = await run_sync_io(_load_registry)
        resource = next(
            (item for item in payload["resources"] if item.get("resource_id") == resource_id),
            None,
        )
        if resource is None:
            raise ValueError("Resource not found")
        if resource.get("is_builtin") or resource.get("creator_username") != sender_username:
            raise PermissionError("Only the resource creator can share this resource")
        requests: list[dict[str, Any]] = []
        recipients = set(resource.get("recipients") or [])
        for recipient in dict.fromkeys(recipient_usernames):
            if recipient == sender_username or recipient in recipients:
                continue
            existing = next(
                (
                    item
                    for item in payload["share_requests"]
                    if item.get("resource_id") == resource_id
                    and item.get("recipient_username") == recipient
                    and item.get("status") == "pending"
                ),
                None,
            )
            if existing:
                continue
            request = {
                "request_id": str(uuid.uuid4()),
                "resource_id": resource_id,
                "sender_username": sender_username,
                "recipient_username": recipient,
                "status": "pending",
                "created_at": time.time(),
            }
            payload["share_requests"].append(request)
            requests.append(copy.deepcopy(request))
        await run_sync_io(_save_registry, payload)
    return requests


async def grant_resource(
    *, resource_id: str, sender_username: str, recipient_usernames: list[str]
) -> list[str]:
    """Immediately grant a custom tool to recipients.

    Tools are executable configuration, so they are not copied through the
    skill approval inbox. The same non-forwardable creator rule still applies.
    """
    async with _LOCK:
        payload = await run_sync_io(_load_registry)
        resource = next(
            (item for item in payload["resources"] if item.get("resource_id") == resource_id),
            None,
        )
        if resource is None:
            raise ValueError("Resource not found")
        if resource.get("is_builtin") or resource.get("creator_username") != sender_username:
            raise PermissionError("Only the resource creator can share this resource")
        recipients = resource.setdefault("recipients", [])
        granted: list[str] = []
        for username in dict.fromkeys(recipient_usernames):
            if username != sender_username and username not in recipients:
                recipients.append(username)
                granted.append(username)
        await run_sync_io(_save_registry, payload)
    return granted


def build_custom_tool_descriptors(username: str) -> list[Any]:
    """Build callable HTTP-tool descriptors visible to a single account."""
    from ..runtime.tool_registry import ToolDescriptor, ToolUISpec

    descriptors: list[Any] = []
    for record in list_resources_for_user_sync(kind="tool", username=username):
        metadata = record.get("metadata") or {}
        url = str(metadata.get("url", "")).strip()
        if not url:
            continue
        name = str(record.get("name", "")).strip()
        if not name:
            continue
        method = str(metadata.get("method", "POST")).upper()
        headers = metadata.get("headers") if isinstance(metadata.get("headers"), dict) else {}

        def _make_invoke(
            endpoint: str,
            request_method: str,
            request_headers: dict[str, Any],
        ):
            def invoke(payload: str = "") -> str:
                headers_copy = {str(key): str(value) for key, value in request_headers.items()}
                data = payload.encode("utf-8") if payload and request_method != "GET" else None
                if data and "Content-Type" not in headers_copy:
                    headers_copy["Content-Type"] = "application/json"
                req = Request(endpoint, data=data, headers=headers_copy, method=request_method)
                try:
                    with urlopen(req, timeout=30) as response:  # nosec B310 - user-defined intranet tool
                        return response.read(20000).decode("utf-8", errors="replace")
                except HTTPError as exc:
                    return f"HTTP {exc.code}: {exc.read(4000).decode('utf-8', errors='replace')}"
                except URLError as exc:
                    return f"Request failed: {exc.reason}"

            return invoke

        invoke = _make_invoke(url, method, headers)
        invoke.__name__ = name
        invoke.__doc__ = str(record.get("description") or metadata.get("description") or name)
        descriptors.append(
            ToolDescriptor(
                name=name,
                func=invoke,
                enabled_by_default=True,
                description=invoke.__doc__ or name,
                ui=ToolUISpec(
                    description=invoke.__doc__ or name,
                    icon="🔗",
                ),
            )
        )
    return descriptors


async def decide_share_request(
    *, request_id: str, recipient_username: str, approve: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Atomically resolve an approval request and return it with its resource."""
    async with _LOCK:
        payload = await run_sync_io(_load_registry)
        request = next(
            (item for item in payload["share_requests"] if item.get("request_id") == request_id),
            None,
        )
        if request is None:
            raise ValueError("Share request not found")
        if request.get("recipient_username") != recipient_username:
            raise PermissionError("This share request belongs to another user")
        if request.get("status") != "pending":
            raise ValueError("Share request has already been processed")
        resource = next(
            (item for item in payload["resources"] if item.get("resource_id") == request.get("resource_id")),
            None,
        )
        if resource is None:
            raise ValueError("Shared resource no longer exists")
        if resource.get("creator_username") != request.get("sender_username"):
            raise PermissionError("Only the original creator may share a resource")
        request["status"] = "approved" if approve else "rejected"
        request["decided_at"] = time.time()
        if approve:
            recipients = resource.setdefault("recipients", [])
            if recipient_username not in recipients:
                recipients.append(recipient_username)
        await run_sync_io(_save_registry, payload)
    return copy.deepcopy(request), copy.deepcopy(resource)
