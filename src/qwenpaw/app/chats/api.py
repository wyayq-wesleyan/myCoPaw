# -*- coding: utf-8 -*-
"""Chat management API."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote
from uuid import uuid4
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel, Field

from agentscope.message import Msg
from agentscope.state import AgentState

from .repo.json_repo import JsonChatRepository
from .session import SafeJSONSession, session_filename
from .manager import ChatManager, MAX_BATCH_SIZE
from .models import (
    BatchArchiveResult,
    ChatSpec,
    ChatUpdate,
    ChatHistory,
)
from ...utils.io_utils import (
    read_json,
    run_sync_io,
    write_json_atomic_async,
)
from ...utils.io_pools import POOL_FILE
from .utils import agentscope_msg_to_message, parse_legacy_memory_state
from ...services.project_directory import (
    resolve_effective_project_dir,
    session_project_dir,
)
from ...checkpoints.runtime import RUNTIME as CHECKPOINT_RUNTIME
from ...config.utils import load_config
from ..auth import list_users
from ..user_resources import (
    create_share_requests,
    notify_resource_share,
    register_resource,
    session_resource_storage_dir,
)
from ..user_space import get_request_username, is_agent_owned

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/chats", tags=["chats"])


async def get_workspace(request: Request):
    """Get the workspace for the active agent."""
    from ..agent_context import get_agent_for_request

    return await get_agent_for_request(request)


async def get_chat_manager(
    request: Request,
) -> ChatManager:
    """Get the chat manager for the active agent.

    Args:
        request: FastAPI request object

    Returns:
        ChatManager instance for the specified agent

    Raises:
        HTTPException: If manager is not initialized
    """
    workspace = await get_workspace(request)
    return workspace.chat_manager


async def get_session(
    request: Request,
) -> SafeJSONSession:
    """Get the session for the active agent.

    Args:
        request: FastAPI request object

    Returns:
        SafeJSONSession instance for the specified agent

    Raises:
        HTTPException: If session is not initialized
    """
    workspace = await get_workspace(request)
    return workspace.session


class ProjectDirectoryUpdate(BaseModel):
    """Controlled Session project directory update."""

    project_dir: str


class ShareChatRequest(BaseModel):
    """Users selected to receive a snapshot of one chat."""

    target_usernames: list[str] = Field(default_factory=list)


async def _project_directory_response(chat: ChatSpec, workspace) -> dict:
    """Build the effective Session project directory response."""
    from ...config.config import load_agent_config

    def _build() -> dict:
        agent_config = load_agent_config(workspace.agent_id)
        project_dir, source = resolve_effective_project_dir(
            workspace.workspace_dir,
            agent_project_dir=agent_config.project_dir,
            session_override=session_project_dir(chat.meta),
        )
        return {
            "project_dir": str(project_dir),
            "source": source,
            "agent_project_dir": agent_config.project_dir,
            "exists": project_dir.is_dir(),
        }

    return await asyncio.to_thread(_build)


@router.get("", response_model=list[ChatSpec])
async def list_chats(
    user_id: Optional[str] = Query(None, description="Filter by user ID"),
    channel: Optional[str] = Query(None, description="Filter by channel"),
    archived: Optional[bool] = Query(
        None,
        description=(
            "Filter by archived status. "
            "false=active only, true=archived only, "
            "null/omit=all (default)"
        ),
    ),
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
):
    """List all chats with optional filters.

    When ``archived`` is omitted, returns all chats (both active and archived).
    Pass ``archived=false`` for active only,
    ``archived=true`` for archived only.
    """
    chats = await mgr.list_chats(
        user_id=user_id,
        channel=channel,
        archived=archived,
    )
    tracker = workspace.task_tracker
    result = []
    for spec in chats:
        status = await tracker.get_status(spec.id)
        result.append(spec.model_copy(update={"status": status}))
    return result


@router.post("", response_model=ChatSpec)
async def create_chat(
    request: ChatSpec,
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Create a new chat.

    Server generates chat_id (UUID) automatically.

    Args:
        request: Chat creation request
        mgr: Chat manager dependency

    Returns:
        Created chat spec with UUID
    """
    chat_id = str(uuid4())
    spec = ChatSpec(
        id=chat_id,
        name=request.name,
        session_id=request.session_id,
        user_id=request.user_id,
        channel=request.channel,
        meta=request.meta,
    )
    return await mgr.create_chat(spec)


async def _load_owned_chat(
    chat_id: str,
    request: Request,
    mgr: ChatManager,
    workspace: Any,
) -> tuple[str | None, ChatSpec]:
    """Resolve *chat_id* to a chat the caller is allowed to act on.

    Ownership is the agent profile's ``owner_username`` rather than anything on
    the chat itself: chats live inside one agent's workspace, so "this agent
    belongs to you" is what actually decides access.

    An **unresolvable** caller (no token) is deliberately not a 401 here. When
    authentication is enabled the middleware has already rejected the request
    before it reaches a handler, so reaching this with no username means the
    install has authentication turned off — the offline single-user profile —
    and ``is_agent_owned`` documents that such callers keep the historical
    unrestricted view. Rejecting them would break export/import on exactly the
    deployment that has no login screen to log into. Endpoints that genuinely
    need a *named* sender (``share_chat``) still require one, they just say so
    themselves.
    """
    username = get_request_username(request)
    chat = await mgr.get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    if not is_agent_owned(load_config(), username, workspace.agent_id):
        raise HTTPException(status_code=403, detail="You do not own this chat")
    return username, chat


@router.post("/{chat_id}/share")
async def share_chat(
    chat_id: str,
    payload: ShareChatRequest,
    request: Request,
    mgr: ChatManager = Depends(get_chat_manager),
    session: SafeJSONSession = Depends(get_session),
    workspace=Depends(get_workspace),
) -> dict:
    """Send a point-in-time chat snapshot to selected users for approval."""
    username, chat = await _load_owned_chat(chat_id, request, mgr, workspace)
    # Sharing is the one chat action that cannot work without a named sender:
    # the snapshot has to be attributed and the sender excluded from the
    # recipients. Unrelated to ownership, so it is checked here rather than in
    # the shared guard (see its docstring).
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")

    users = {str(item["username"]): item for item in list_users()}
    targets = [
        target
        for target in dict.fromkeys(payload.target_usernames)
        if target in users
        and users[target].get("role") != "admin"
        and target != username
    ]
    if not targets:
        raise HTTPException(status_code=400, detail="No eligible recipients selected")

    # Every send gets its own resource and snapshot. Reusing a previous
    # resource would let a later share silently change an already-pending
    # request's contents.
    resource = await register_resource(
        kind="session",
        name=str(chat.name or chat.id),
        creator_username=username,
        metadata={
            "chat_spec": chat.model_dump(mode="json"),
            "source_agent_id": workspace.agent_id,
        },
    )
    snapshot_dir = session_resource_storage_dir(str(resource["resource_id"]))
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    source_path = Path(
        await asyncio.to_thread(
            session._get_save_path,
            chat.session_id,
            chat.user_id,
            chat.channel,
        )
    )
    snapshot_path = snapshot_dir / "state.json"
    if source_path.is_file():
        await asyncio.to_thread(shutil.copy2, source_path, snapshot_path)
    else:
        await asyncio.to_thread(snapshot_path.write_text, "{}", "utf-8")

    requests = await create_share_requests(
        resource_id=str(resource["resource_id"]),
        sender_username=username,
        recipient_usernames=targets,
    )
    for item in requests:
        await notify_resource_share(request_item=item, resource=resource)
    return {"created": len(requests), "skipped": len(targets) - len(requests)}


# ---------------------------------------------------------------------------
# Session export / import (a file the user keeps, not a share to another user)
# ---------------------------------------------------------------------------

# Marker and version written into every exported file. The marker is checked on
# import so an unrelated JSON file gets a readable rejection instead of being
# silently half-imported.
_SESSION_FILE_FORMAT = "huizhi.chat-session"
_SESSION_FILE_VERSION = 1
# Cap on an uploaded session file. A long conversation with verbose tool output
# is a few MB; the limit exists so a wrong or hostile file cannot turn one
# import into an out-of-memory.
_SESSION_IMPORT_MAX_BYTES = 64 * 1024 * 1024


def _rows_for_export(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the source session's identity from transcript rows.

    ``export_session_rows`` is a faithful row reader and returns ``session_id``
    and ``agent_id`` along with everything else. Those two describe *where the
    rows live here*, not the conversation, and the importer overwrites both, so
    carrying them into a file that may be handed to someone else leaks the
    exporter's session and agent identifiers for no benefit.

    ``seq`` is kept: it is the source's write order, it cannot be mistaken for an
    identity, and it makes the file readable when something looks wrong.
    ``dedup_key`` is kept because the importer uses it to stay idempotent.
    """
    return [
        {key: value for key, value in row.items() if key not in ("session_id", "agent_id")}
        for row in rows
    ]


def _scroll_strategy_enabled(workspace: Any) -> bool:
    """Whether this agent keeps a durable transcript outside the session file."""
    return _scroll_history_store_path(workspace) is not None


def _scroll_history_store_path(workspace: Any) -> Path | None:
    """Where *workspace*'s durable transcript is addressed, or ``None``.

    Always through ``load_agent_config``, never through
    ``load_config().agents.profiles[...]``: the global profile leaves
    ``light_context_config`` unset (``strategy`` is ``None``), because the
    effective value lives in the per-agent config — which is also what the
    Scroll manager itself reads. Reading the global copy reported "not scroll"
    for every agent.

    The returned path is an *address*, not necessarily an existing file: history
    has lived in PostgreSQL since the migration, and the filename is only hashed
    into the partition key. Nothing in this module may treat its existence as a
    signal — see ``_load_durable_transcript``.
    """
    try:
        from ...config.config import load_agent_config

        lcc = load_agent_config(workspace.agent_id).running.light_context_config
    except Exception:  # noqa: BLE001 - unknown agent means "no transcript"
        logger.debug(
            "Could not resolve the context config for agent %s",
            getattr(workspace, "agent_id", "?"),
            exc_info=True,
        )
        return None
    if getattr(lcc, "strategy", "native") != "scroll":
        return None
    return Path(workspace.workspace_dir) / lcc.scroll_config.db_filename


def _read_session_rows(workspace: Any, session_id: str) -> list[dict[str, Any]]:
    """Read one session's durable transcript rows, or ``[]``."""
    db_path = _scroll_history_store_path(workspace)
    if db_path is None:
        return []
    from ...agents.context.scroll.history import HistoryStore

    store = HistoryStore(db_path)
    try:
        return store.export_session_rows(
            session_id,
            getattr(workspace, "agent_id", None),
        )
    finally:
        store.close()


def _write_session_rows(
    workspace: Any,
    *,
    session_id: str,
    rows: list[dict],
) -> int:
    """Write transcript rows into *session_id*, returning the number written."""
    db_path = _scroll_history_store_path(workspace)
    if db_path is None:
        return 0
    from ...agents.context.scroll.history import HistoryStore

    store = HistoryStore(db_path)
    try:
        return store.import_session_rows(
            session_id=session_id,
            rows=rows,
            agent_id=getattr(workspace, "agent_id", None),
        )
    finally:
        store.close()


def _attachment_disposition(filename: str) -> str:
    """Build a ``Content-Disposition`` that survives a non-ASCII filename.

    A bare ``filename="会话.json"`` is mojibake in most browsers because the
    header is read as latin-1. The ``filename*`` form (RFC 5987) carries the
    real UTF-8 name, and the plain ``filename`` is kept as an ASCII-only
    fallback for clients that ignore it.
    """
    ascii_fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "session.json"
    return (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


def _export_file_name(chat_name: str, chat_id: str) -> str:
    """``<name>-<date>.session.json``, safe on every platform."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", (chat_name or "").strip())
    stem = stem.strip(". ") or chat_id
    # Long names would exceed the filesystem limit once the suffix is added.
    return f"{stem[:80]}-{stamp}.session.json"


@router.get("/{chat_id}/export")
async def export_chat(
    chat_id: str,
    request: Request,
    mgr: ChatManager = Depends(get_chat_manager),
    session: SafeJSONSession = Depends(get_session),
    workspace=Depends(get_workspace),
) -> Response:
    """Download one conversation as a self-contained JSON file.

    The file carries two things, and it needs both. ``state`` is the live agent
    context, so an imported copy keeps talking with the same memory and settings.
    ``transcript`` is the durable row set — with the default ``scroll`` strategy
    the console renders history from those rows, *not* from the state file, so
    exporting only the state would silently truncate a long conversation to the
    last context window.

    Nothing identifying is written: no ``id``, ``session_id``, ``user_id`` or
    owner — neither in the chat block nor in the transcript rows, where
    ``session_id``/``agent_id`` are dropped by ``_rows_for_export``. Import mints
    all of those, and omitting them here means a file can never be used to claim
    an identity in someone else's account.
    """
    _username, chat = await _load_owned_chat(chat_id, request, mgr, workspace)

    state = await session.get_session_state_dict(
        chat.session_id,
        chat.user_id,
        chat.channel,
    )

    transcript: list[dict[str, Any]] = []
    if _scroll_strategy_enabled(workspace):
        try:
            transcript = _rows_for_export(
                await run_sync_io(
                    _read_session_rows,
                    workspace,
                    chat.session_id,
                ),
            )
        except Exception:  # noqa: BLE001 - a session without history is still exportable
            logger.warning(
                "Could not read the durable transcript for chat %s; "
                "exporting the live window only",
                chat_id,
                exc_info=True,
            )

    payload = {
        "format": _SESSION_FILE_FORMAT,
        "version": _SESSION_FILE_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "chat": {
            "name": chat.name,
            "channel": chat.channel,
            "created_at": chat.created_at.isoformat() if chat.created_at else None,
            "updated_at": chat.updated_at.isoformat() if chat.updated_at else None,
        },
        "session": {
            "strategy": "scroll" if transcript else "native",
            "state": state or {},
            "transcript": transcript,
        },
    }
    body = json.dumps(payload, ensure_ascii=False, default=str)
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": _attachment_disposition(
                _export_file_name(chat.name, chat.id),
            ),
            "Cache-Control": "no-store",
        },
    )


@router.post("/import", response_model=ChatSpec)
async def import_chat(
    request: Request,
    file: UploadFile = File(...),
    mgr: ChatManager = Depends(get_chat_manager),
    session: SafeJSONSession = Depends(get_session),
    workspace=Depends(get_workspace),
) -> ChatSpec:
    """Create a new conversation from a previously exported file.

    Always a **new** session: the id, session id and user id are minted here, so
    importing cannot overwrite, resume into, or impersonate an existing chat,
    and importing the same file twice yields two independent conversations.

    Everything is written into the caller's own agent workspace — the same
    boundary every other chat endpoint uses. Nothing from the file is used to
    build a filesystem path; the path comes from the freshly generated session
    id, so a crafted file cannot escape the workspace.
    """
    username = get_request_username(request)
    if not is_agent_owned(load_config(), username, workspace.agent_id):
        raise HTTPException(
            status_code=403,
            detail="You do not own this agent",
        )

    raw = await file.read()
    if len(raw) > _SESSION_IMPORT_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                "Session file is larger than "
                f"{_SESSION_IMPORT_MAX_BYTES // (1024 * 1024)} MB"
            ),
        )
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="Not a valid session file: the upload is not UTF-8 JSON",
        ) from exc
    if not isinstance(payload, dict) or payload.get("format") != _SESSION_FILE_FORMAT:
        raise HTTPException(
            status_code=400,
            detail="Not a session export file (missing the expected format marker)",
        )
    version = payload.get("version")
    if version != _SESSION_FILE_VERSION:
        raise HTTPException(
            status_code=400,
            detail=(
                "Unsupported session file version: "
                f"{version!r} (this build reads version {_SESSION_FILE_VERSION})"
            ),
        )
    session_block = payload.get("session")
    if not isinstance(session_block, dict):
        raise HTTPException(status_code=400, detail="Session file has no session body")
    state = session_block.get("state")
    if not isinstance(state, dict):
        raise HTTPException(status_code=400, detail="Session file has no state")
    transcript = session_block.get("transcript")
    if not isinstance(transcript, list):
        transcript = []

    chat_block = payload.get("chat")
    if not isinstance(chat_block, dict):
        chat_block = {}
    source_name = str(chat_block.get("name") or "").strip()

    # The channel is deliberately not taken from the file. It selects a
    # subdirectory and a channel badge, and it means "which inbound channel this
    # conversation arrived on" — not where a file happened to come from. An
    # imported copy is a console conversation, which is also one fewer
    # attacker-controlled string anywhere near a path.
    from ..channels.schema import DEFAULT_CHANNEL

    new_session_id = f"console:imported:{uuid4()}"
    new_spec = ChatSpec(
        id=str(uuid4()),
        name=source_name or "Imported chat",
        session_id=new_session_id,
        user_id="default",
        channel=DEFAULT_CHANNEL,
        meta={
            "imported_from_file": True,
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "source_agent_id": (
                str(payload.get("agent", {}).get("agent_id") or "")
                if isinstance(payload.get("agent"), dict)
                else ""
            ),
            "source_channel": str(chat_block.get("channel") or ""),
        },
    )

    # Order matters: transcript first, registration last. A failure partway then
    # leaves a file the user cannot see rather than a chat in the list that
    # opens to an empty or half-written conversation.
    written_rows = 0
    if transcript and _scroll_strategy_enabled(workspace):
        try:
            written_rows = await run_sync_io(
                _write_session_rows,
                workspace,
                session_id=new_session_id,
                rows=transcript,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Session import failed writing the transcript: %s",
                exc,
                exc_info=True,
            )
            # Include the cause. This is an operator-facing action on the
            # caller's own file, and the reason is nearly always a deployment
            # problem (an unreachable platform database, a schema that did not
            # build) rather than anything the file did. "Could not write the
            # imported conversation history" on its own sent someone to the
            # server logs to find out what it meant; the same sentence plus
            # `relation "platform.history_seq" does not exist` is immediately
            # actionable.
            reason = f"{type(exc).__name__}: {exc}".replace("\n", " ").strip()
            raise HTTPException(
                status_code=500,
                detail=f"Could not write the imported conversation history ({reason})",
            ) from exc

    session_path = Path(
        await run_sync_io(
            session._get_save_path,
            new_session_id,
            new_spec.user_id,
            new_spec.channel,
        )
    )
    try:
        await write_json_atomic_async(
            session_path,
            state,
            indent=None,
            pool=POOL_FILE,
        )
        await mgr.create_chat(new_spec)
    except Exception as exc:  # noqa: BLE001
        # Do not leave a state file behind for a chat that was never registered:
        # it would be an orphan on disk with no way for the user to see or
        # remove it.
        try:
            await run_sync_io(session_path.unlink, True)
        except OSError:
            logger.debug("Could not clean up %s", session_path, exc_info=True)
        logger.error("Session import failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Could not create the imported conversation",
        ) from exc

    logger.info(
        "Imported session %s as chat %s (%d transcript rows)",
        chat_block.get("name") or "-",
        new_spec.id,
        written_rows,
    )
    return new_spec


@router.post("/batch-delete", response_model=dict)
async def batch_delete_chats(
    chat_ids: list[str],
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
):
    """Delete chats by chat IDs.

    Args:
        chat_ids: List of chat IDs
        mgr: Chat manager dependency
    Returns:
        True if deleted, False if failed

    """
    chats = {chat.id: chat for chat in await mgr.list_chats(archived=None)}
    deleted = await mgr.delete_chats(chat_ids=chat_ids)
    if deleted:
        await CHECKPOINT_RUNTIME.delete_session_checkpoints(
            workspace,
            [
                (chat.session_id, chat.user_id, chat.channel)
                for chat_id in chat_ids
                if (chat := chats.get(chat_id)) is not None
            ],
        )
    return {"deleted": deleted}


# ----- Archive endpoints -----


class BatchChatIds(BaseModel):
    """Request body for batch archive/unarchive."""

    chat_ids: list[str] = Field(
        ...,
        max_length=MAX_BATCH_SIZE,
        description="List of chat IDs to process",
    )


@router.post("/actions/batch-archive", response_model=BatchArchiveResult)
async def batch_archive_chats(
    payload: BatchChatIds,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
):
    """Batch archive chats. Running chats are skipped."""
    tracker = workspace.task_tracker
    return await mgr.batch_archive(
        chat_ids=payload.chat_ids,
        get_status=tracker.get_status,
    )


@router.post("/actions/batch-unarchive", response_model=BatchArchiveResult)
async def batch_unarchive_chats(
    payload: BatchChatIds,
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Batch unarchive chats."""
    return await mgr.batch_unarchive(chat_ids=payload.chat_ids)


@router.post("/{chat_id}/archive", response_model=ChatSpec)
async def archive_chat(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
):
    """Archive a single chat. Idempotent.

    Returns 409 if the chat is currently running.
    """
    status = await workspace.task_tracker.get_status(chat_id)
    try:
        result = await mgr.archive_chat(chat_id, check_status=status)
    except ValueError as e:
        raise HTTPException(
            status_code=409,
            detail="Chat is currently in progress, cannot archive",
        ) from e
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    return result


@router.post("/{chat_id}/unarchive", response_model=ChatSpec)
async def unarchive_chat(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Unarchive a single chat. Idempotent."""
    result = await mgr.unarchive_chat(chat_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    return result


@router.get("/{chat_id}/project-dir")
async def get_chat_project_dir(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> dict:
    """Return the Session override and effective project directory."""
    chat = await mgr.get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return await _project_directory_response(chat, workspace)


@router.put("/{chat_id}/project-dir")
async def set_chat_project_dir(
    chat_id: str,
    body: ProjectDirectoryUpdate,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> dict:
    """Persist a validated Session project directory override."""

    def _resolve_target() -> Path:
        target = Path(body.project_dir).expanduser().resolve()
        if not target.is_dir():
            raise NotADirectoryError(str(target))
        return target

    try:
        target = await asyncio.to_thread(_resolve_target)
    except NotADirectoryError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Project directory is unavailable: {exc}",
        ) from exc
    chat = await mgr.set_project_dir(chat_id, str(target))
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return await _project_directory_response(chat, workspace)


@router.delete("/{chat_id}/project-dir")
async def clear_chat_project_dir(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> dict:
    """Clear the override and inherit the Agent default project directory."""
    chat = await mgr.set_project_dir(chat_id, None)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return await _project_directory_response(chat, workspace)


# ----- Existing CRUD endpoints -----


def _load_durable_transcript(
    workspace: Any,
    chat_spec: Any,
) -> list[Msg] | None:
    """Return the session's full durable transcript, or ``None`` if unavailable.

    Only the Scroll context strategy keeps one; for any other strategy (or a
    workspace whose history is unreadable) the caller falls back to the live
    window. Blocking on PostgreSQL, so callers dispatch it to a thread.

    Deliberately **no file-existence check**. Until the storage move, history was
    a per-workspace ``history.db`` and this guarded on ``is_file()``; history now
    lives in PostgreSQL, so that file is only present on installs old enough to
    have migrated, and the guard silently reduced every *fresh* install to the
    live window — evicted turns disappeared from the console again, which is the
    exact symptom this function was written to fix. The path is an address, not a
    file: ``HistoryStore`` hashes it into the partition key and never opens it.
    """
    try:
        db_path = _scroll_history_store_path(workspace)
        if db_path is None:
            return None
        from ...agents.context.scroll.history import HistoryStore

        store = HistoryStore(db_path)
        try:
            return store.load_session_messages(
                chat_spec.session_id,
                getattr(workspace, "agent_id", None),
            )
        finally:
            store.close()
    except Exception:  # noqa: BLE001 - never fail a read over the transcript
        logger.debug(
            "Could not read the durable transcript for chat %s",
            getattr(chat_spec, "id", "?"),
            exc_info=True,
        )
        return None


@router.get("/{chat_id}", response_model=ChatHistory)
async def get_chat(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    session: SafeJSONSession = Depends(get_session),
    workspace=Depends(get_workspace),
):
    """Get detailed information about a specific chat by UUID.

    Args:
        request: FastAPI request (for agent context)
        chat_id: Chat UUID
        mgr: Chat manager dependency
        session: SafeJSONSession dependency

    Returns:
        ChatHistory with messages and status (idle/running)

    Raises:
        HTTPException: If chat not found (404)
    """
    chat_spec = await mgr.get_chat(chat_id)
    if not chat_spec:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )

    state = await session.get_session_state_dict(
        chat_spec.session_id,
        chat_spec.user_id,
        chat_spec.channel,
    )
    backend = workspace.config.backend
    context = ((state.get("agent") or {}).get("state") or {}).get("context")
    if not context and backend != "qwenpaw":
        try:
            await workspace.harness_runtime.hydrate_session(
                backend=backend,
                session_id=chat_spec.session_id,
                user_id=chat_spec.user_id,
                channel=chat_spec.channel,
                settings=dict(workspace.config.backend_settings),
            )
            state = await session.get_session_state_dict(
                chat_spec.session_id,
                chat_spec.user_id,
                chat_spec.channel,
            )
        except Exception:
            logger.debug(
                "Third-party session recovery failed for %s",
                chat_spec.session_id,
                exc_info=True,
            )
    status = await workspace.task_tracker.get_status(chat_id)

    # Prefer the durable transcript over the live context window. Scroll evicts
    # older turns from the window as it fills, so returning the window made the
    # console drop every evicted turn: the right-hand anchor list shrank during
    # a long conversation and "jump to top" landed mid-session even though the
    # stored name, data and messages were all intact. The window remains the
    # fallback for non-Scroll strategies and unreadable history.
    #
    # This runs BEFORE the missing-state early return on purpose: a session
    # whose state file was rotated away or lost across a restart still has its
    # durable transcript, and returning "no messages" for it would hide a
    # conversation that is fully intact on disk.
    durable = await run_sync_io(
        _load_durable_transcript,
        workspace,
        chat_spec,
    )
    if durable:
        return ChatHistory(
            messages=agentscope_msg_to_message(durable),
            status=status,
        )

    if not state:
        return ChatHistory(messages=[], status=status)

    agent_raw = state.get("agent", {})
    memories: list[Msg] = []

    state_raw = agent_raw.get("state")
    if isinstance(state_raw, dict):
        try:
            agent_state = AgentState.model_validate(state_raw)
            memories = list(agent_state.context)
        except Exception:
            logger.debug(
                "Failed to parse agent.state, falling back to legacy",
                exc_info=True,
            )

    # Legacy fallback: 1.x ``agent.memory`` format.
    if not memories:
        memory_raw = agent_raw.get("memory", {})
        if memory_raw:
            memories, _summary = parse_legacy_memory_state(memory_raw)

    messages = agentscope_msg_to_message(memories)
    return ChatHistory(messages=messages, status=status)


@router.put("/{chat_id}", response_model=ChatSpec)
async def update_chat(
    chat_id: str,
    spec: ChatUpdate,
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Update an existing chat.

    Args:
        chat_id: Chat UUID
        spec: Partial chat update payload
        mgr: Chat manager dependency

    Returns:
        Updated chat spec

    Raises:
        HTTPException: If chat not found (404)
    """
    # lock_name: this is the user renaming the chat themselves, so the name
    # becomes final. Everything the user can see (chat drawer, session
    # management) renames through this endpoint, and none of it is automatic.
    updated = await mgr.patch_chat(chat_id, spec, lock_name=True)
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    return updated


@router.delete("/{chat_id}", response_model=dict)
async def delete_chat(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
):
    """Delete a chat by UUID.

    Note: This only deletes the chat spec (UUID mapping).
    JSONSession state is NOT deleted.

    Args:
        chat_id: Chat UUID
        mgr: Chat manager dependency

    Returns:
        True if deleted, False if failed

    Raises:
        HTTPException: If chat not found (404)
    """
    chat = await mgr.get_chat(chat_id)
    deleted = await mgr.delete_chats(chat_ids=[chat_id])
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    if chat is not None:
        await CHECKPOINT_RUNTIME.delete_session_checkpoints(
            workspace,
            [(chat.session_id, chat.user_id, chat.channel)],
        )
    return {"deleted": True}


class MessageSearchHit(BaseModel):
    """One matching message, located for the user."""

    chat_id: str = Field(description="Chat UUID to open")
    chat_name: str = Field(default="", description="Chat display name")
    agent_id: str = Field(description="Agent the chat belongs to")
    session_id: str = Field(description="Session identifier")
    role: str = Field(default="", description="user or assistant")
    created_at: str | None = Field(default=None)
    snippet: str = Field(default="", description="Text around the match")
    matched_terms: int = Field(default=0, description="Distinct query terms hit")


class MessageSearchResponse(BaseModel):
    query: str
    hits: list[MessageSearchHit]
    scanned_chats: int
    truncated: bool = Field(
        default=False,
        description="True when the scan stopped at the internal cap",
    )


# Bound on how many files a single search will read, so a large deployment
# cannot turn one query into an unbounded disk sweep.
_SEARCH_MAX_CHATS = 2000
_SEARCH_MAX_HITS = 100
# Characters of context returned around a match.
_SEARCH_SNIPPET_PAD = 80


def _message_text(content: Any) -> str:
    """Flatten one message's content into searchable text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            # AgentScope blocks (TextBlock etc.) expose ``text``.
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _search_terms(query: str) -> list[str]:
    """Split a query into terms; whitespace-separated, case-folded."""
    return [term for term in query.casefold().split() if term]


def _scan_session_file(
    path: Path,
    chat_by_session: dict[str, Any],
    agent_id: str,
    terms: list[str],
) -> list[MessageSearchHit]:
    """Return matches inside one session file."""
    try:
        payload = read_json(path)
    except Exception:  # noqa: BLE001 - a damaged file must not fail the search
        logger.debug("message search: unreadable session file %s", path)
        return []
    if not isinstance(payload, dict):
        return []

    context = (
        ((payload.get("agent") or {}).get("state") or {}).get("context")
        if isinstance(payload.get("agent"), dict)
        else None
    )
    if not isinstance(context, list):
        return []

    hits: list[MessageSearchHit] = []
    for message in context:
        if not isinstance(message, dict):
            continue
        text = _message_text(message.get("content"))
        if not text:
            continue
        folded = text.casefold()
        matched = [term for term in terms if term in folded]
        # Every term must be present: a multi-word query narrows the result
        # ("光刻机 供应链" must not match a message that only says 光刻机).
        if len(matched) != len(terms):
            continue

        # Snippet around the first match.
        pos = folded.find(matched[0])
        start = max(0, pos - _SEARCH_SNIPPET_PAD)
        end = min(len(text), pos + len(matched[0]) + _SEARCH_SNIPPET_PAD)
        snippet = text[start:end].replace("\n", " ").strip()
        if start > 0:
            snippet = "…" + snippet
        if end < len(text):
            snippet = snippet + "…"

        session_id = str(
            chat_by_session.get("session_id") or path.stem,
        )
        hits.append(
            MessageSearchHit(
                chat_id=str(chat_by_session.get("id") or ""),
                chat_name=str(chat_by_session.get("name") or ""),
                agent_id=agent_id,
                session_id=session_id,
                role=str(message.get("role") or ""),
                created_at=(
                    str(message.get("created_at"))
                    if message.get("created_at")
                    else None
                ),
                snippet=snippet,
                matched_terms=len(matched),
            ),
        )
    return hits


@router.get("/search", response_model=MessageSearchResponse)
async def search_messages(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(50, ge=1, le=100),
):
    """Search message text across the caller's own chats and agents.

    Answers "I remember a phrase but not which agent or chat it was in". The
    scan is confined to the caller's own agents, resolved from the account, so
    it cannot reach another account's conversations.

    Matching is a case-insensitive substring test over each message's text;
    multi-word queries require every term to appear in the same message.

    Ownership is filtered inline rather than through ``user_agent_ids``: that
    helper runs ``_migrate_user_workspace_paths``, which *moves* workspace
    directories into the account layout. A search must not write to the
    filesystem, so this stays a pure read.
    """
    from ...config.utils import load_config
    from ..user_space import get_request_username

    username = get_request_username(request)
    config = load_config()
    if not username:
        return MessageSearchResponse(query=q, hits=[], scanned_chats=0)
    owned_agents = [
        agent_id
        for agent_id, profile in config.agents.profiles.items()
        if getattr(profile, "owner_username", None) == username
    ]
    if not owned_agents:
        return MessageSearchResponse(query=q, hits=[], scanned_chats=0)

    terms = _search_terms(q)
    if not terms:
        return MessageSearchResponse(query=q, hits=[], scanned_chats=0)

    hits: list[MessageSearchHit] = []
    scanned = 0
    truncated = False

    for agent_id in owned_agents:
        if len(hits) >= limit or scanned >= _SEARCH_MAX_CHATS:
            truncated = scanned >= _SEARCH_MAX_CHATS
            break
        profile = config.agents.profiles.get(agent_id)
        if profile is None:
            continue
        workspace_dir = Path(str(profile.workspace_dir)).expanduser()

        # Chat metadata (name/id) is needed to label and open each hit.
        chat_by_session: dict[str, dict[str, Any]] = {}
        try:
            repo = JsonChatRepository(workspace_dir / "chats.json")
            chats_file = await repo.load()
            for spec in chats_file.chats:
                # Index by the file name the session store actually uses
                # (``<user>_<session>.json``), not by the bare session id: the
                # two differ, so keying on the id would leave every hit with no
                # chat to open.
                key = session_filename(spec.session_id, spec.user_id)
                chat_by_session[key] = {
                    "id": spec.id,
                    "name": spec.name,
                    "session_id": spec.session_id,
                }
        except Exception:  # noqa: BLE001
            logger.debug("message search: no chats.json for %s", agent_id)

        sessions_dir = workspace_dir / "sessions"
        if not sessions_dir.is_dir():
            continue
        for path in sorted(sessions_dir.rglob("*.json")):
            if len(hits) >= limit or scanned >= _SEARCH_MAX_CHATS:
                truncated = truncated or scanned >= _SEARCH_MAX_CHATS
                break
            scanned += 1
            meta = chat_by_session.get(path.name, {})
            for hit in await run_sync_io(
                _scan_session_file,
                path,
                meta,
                agent_id,
                terms,
            ):
                hits.append(hit)
                if len(hits) >= limit:
                    break

    hits.sort(key=lambda h: (h.created_at or ""), reverse=True)
    return MessageSearchResponse(
        query=q,
        hits=hits[:limit],
        scanned_chats=scanned,
        truncated=truncated,
    )
