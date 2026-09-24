# -*- coding: utf-8 -*-
"""Pluggable context-management strategies.

The default agent behavior is AgentScope-native compression; injecting a
:class:`ContextManager` replaces it. The only built-in alternative today is the
*scroll* strategy (durable conversation history in the platform PostgreSQL store
+ an in-context eviction index + a structured ``recall_history`` recall tool),
selected via ``LightContextConfig.strategy == "scroll"``.

:func:`build_scroll_components` is the single entry point the builder calls; it
returns ``None`` for any non-scroll strategy, so the feature is fully opt-in.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import ContextManager

logger = logging.getLogger(__name__)

__all__ = [
    "ContextManager",
    "ScrollComponents",
    "build_scroll_components",
    "scroll_unsandboxed_allowed",
]

# Deployment-layer gate for the unsandboxed-recall escape hatch. Only an
# operator who can set process env vars may flip this on — never an agent.json
# / API payload. See scroll_unsandboxed_allowed.
_UNSANDBOXED_ENV = "QWENPAW_ALLOW_UNSANDBOXED_RECALL"
_TRUTHY = {"1", "true", "yes", "on"}


def scroll_unsandboxed_allowed(scroll_config: Any) -> bool:
    """Whether scroll's recall REPL may run WITHOUT a sandbox.

    SECURITY: running recall unsandboxed executes model-authored Python as the
    agent user with zero isolation. In a multi-tenant deployment an untrusted
    ``agent.json`` / API payload must never be able to turn the sandbox off on
    its own — that would be a privilege-escalation path. So this escape hatch
    is gated by the deployment-layer ``QWENPAW_ALLOW_UNSANDBOXED_RECALL`` env
    var; the per-agent ``scroll_config.allow_unsandboxed`` flag is honored ONLY
    when that env var also grants it. Default-deny: if either is missing,
    recall stays sandboxed (or, with no sandbox available, refuses to run).
    """
    if os.environ.get(_UNSANDBOXED_ENV, "").strip().lower() not in _TRUTHY:
        return False
    return bool(getattr(scroll_config, "allow_unsandboxed", False))


# history.db auto-purges past history_retention_days (default 30), but an
# operator can disable that (set 0) or a very chatty agent can outpace it, so
# the store may still grow large. Warn when it crosses this size. Process-level
# dedupe keeps a long-lived server from re-warning on every agent build.
_DB_SIZE_WARN_BYTES = 1 * 1024**3  # 1 GiB
_DB_SIZE_WARNED: set[str] = set()
# Workspaces already told about the switch this process. Process-level (not
# file-level) because the store is in PostgreSQL now: there is no created file
# whose presence could suppress a repeat on later boots.
_FIRST_RUN_WARNED: set[str] = set()


@dataclass
class ScrollComponents:
    """The pieces the builder wires when the scroll strategy is active."""

    context_manager: Any  # ScrollContextManager (delegated agent hooks)
    recall_tool: Any  # raw structured recall_history fn (in-process, no
    # sandbox) — the front door for expand/search/recall_tool lookups


def _warn_first_run(db_path: Path) -> None:
    """Emit the scroll switch-over notice once per process for a workspace.

    Logged (not raised) so it never blocks startup. The durable store lives in
    the platform PostgreSQL database, so there is no per-workspace file whose
    creation marks the first run — a process-level set keeps the notice to one
    per workspace per boot instead of repeating on every agent build. The path
    is named anyway because it is still the workspace handle callers use (and
    where a pre-migration ``history.db`` may remain).
    """
    key = str(db_path)
    if key in _FIRST_RUN_WARNED:
        return
    _FIRST_RUN_WARNED.add(key)
    logger.warning(
        "scroll is now the DEFAULT context strategy; history is stored in the "
        "platform PostgreSQL database (workspace handle: %s). Conversation "
        "turns evicted from the live window are persisted there and recalled "
        "on demand instead of being summarized in place. To restore the "
        "previous behavior, set running.light_context_config.strategy to "
        '"native" in this agent\'s config (agent.json) and restart.',
        db_path,
    )


def _warn_db_size(db_path: Path) -> None:
    """Warn once per process when ``history.db`` has grown past the threshold.

    Sums the main db and its ``-wal`` sidecar (the bulk of uncommitted growth).
    Log-only and best-effort: a stat failure must never block wiring.
    """
    key = str(db_path)
    if key in _DB_SIZE_WARNED:
        return
    try:
        total = db_path.stat().st_size
        wal = db_path.with_name(db_path.name + "-wal")
        if wal.exists():
            total += wal.stat().st_size
    except OSError:
        return
    if total < _DB_SIZE_WARN_BYTES:
        return
    _DB_SIZE_WARNED.add(key)
    logger.warning(
        "scroll history at %s is %.1f GiB. Rows older than "
        "history_retention_days (default 30) auto-purge on startup and on "
        "teardown; if you set history_retention_days=0 the store keeps "
        "everything and grows without bound. Lower the retention window to "
        "trim it.",
        db_path,
        total / 1024**3,
    )


def build_scroll_components(
    *,
    agent_config: Any,
    workspace_dir: Any,
    model: Any,
    session_id: str,
    agent_id: str | None = None,
    offloader: Any = None,
) -> ScrollComponents | None:
    """Construct the scroll strategy's components, or ``None`` if not selected.

    Returns ``None`` when ``strategy != "scroll"`` or no workspace is
    available, leaving the agent on its native context management.
    """
    try:
        lcc = agent_config.running.light_context_config
    except Exception:
        logger.info("scroll: no light_context_config; staying native")
        return None
    strategy = getattr(lcc, "strategy", "native")
    if strategy != "scroll" or not workspace_dir:
        logger.info(
            "scroll: NOT wiring (strategy=%r, workspace_dir=%r) — native",
            strategy,
            workspace_dir,
        )
        return None
    _ = model  # Kept for builder API compatibility.
    logger.info(
        "scroll: wiring components (workspace_dir=%s, session_id=%s)",
        workspace_dir,
        session_id,
    )

    # Everything below is guarded: if any scroll dependency is unsatisfied
    # (a lazy import fails, e.g. a missing package or a broken submodule) or a
    # component can't be constructed, we log and return ``None`` so the agent
    # silently falls back to native context management instead of failing to
    # build. Native keeps full history in-context, so degrading is always safe.
    history = None
    try:
        # Imported lazily so the native path never pays for the scroll
        # machinery — and so a missing scroll dependency degrades to native
        # here rather than breaking import of this module.
        from .scroll.history import HistoryStore
        from .scroll.manager import ScrollContextManager
        from .scroll.recall_tool import RecallLoopGuard, make_recall_history

        sc = lcc.scroll_config
        trc = lcc.tool_result_pruning_config
        db_path = Path(workspace_dir) / sc.db_filename
        # The store is PostgreSQL now, so there is no per-workspace file whose
        # absence signals a first run. Warn once per process instead: scroll is
        # the default as of this release, so an agent that never set
        # ``strategy`` is switched silently, and the notice is what makes the
        # switch and its rollback path discoverable.
        _warn_first_run(db_path)
        # A leftover legacy history.db is worth flagging: it may still hold
        # pre-migration rows and its size is what the retention warning is about.
        if db_path.exists():
            _warn_db_size(db_path)
        history = HistoryStore(db_path)
        recall_loop_guard = RecallLoopGuard()

        manager = ScrollContextManager(
            history=history,
            session_id=session_id,
            agent_id=agent_id,
            # Legacy dialog archive is opt-in; only hand the manager an
            # offloader when configured, so by default scroll writes nothing
            # to dialog/.
            offloader=(
                offloader if getattr(sc, "offload_dialog", False) else None
            ),
            recall_loop_guard=recall_loop_guard,
        )
        # Structured front door for the common recall ops (expand / search /
        # recall_tool): in-process bound queries against the platform store, no
        # sandbox, no approval. This is the only recall path — PostgreSQL is
        # shared across tenants and has no per-connection authorizer, so the
        # arbitrary-SQL REPL that used to run alongside it is no longer offered.
        recall = make_recall_history(
            history_db_path=str(history.path),
            session_id=session_id,
            agent_id=agent_id,
            loop_guard=recall_loop_guard,
            page_max_bytes=trc.pruning_recent_msg_max_bytes,
        )
        return ScrollComponents(
            context_manager=manager,
            recall_tool=recall,
        )
    except Exception:  # noqa: BLE001 - any scroll failure degrades to native
        if history is not None:
            try:
                history.close()
            except Exception:  # noqa: BLE001 - preserve fallback behavior
                logger.debug(
                    "scroll: failed to close history after wiring failure",
                    exc_info=True,
                )
        logger.warning(
            "scroll: failed to wire components — falling back to native "
            "context management",
            exc_info=True,
        )
        return None
