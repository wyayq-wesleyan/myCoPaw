# -*- coding: utf-8 -*-
"""Helpers for multi-user workspace isolation."""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request

from ..agents.skill_system import get_workspace_skills_dir
from ..agents.utils import copy_workspace_md_files, normalize_agent_language
from ..config.config import (
    AgentProfileConfig,
    AgentProfileRef,
    ChannelConfig,
    HeartbeatConfig,
    MCPConfig,
    ToolsConfig,
    save_agent_config,
)
from ..config.utils import load_config, save_config
from ..constant import WORKING_DIR

logger = logging.getLogger(__name__)

USER_SPACES_DIR = WORKING_DIR / "users"


def _slug_username(username: str) -> str:
    cleaned = []
    for char in (username or "").strip().lower():
        if char.isalnum():
            cleaned.append(char)
        elif char in {"-", "_", "."}:
            cleaned.append(char)
        else:
            cleaned.append("-")
    slug = "".join(cleaned).strip("-._")
    return slug or "user"


def get_request_username(request: Request | None) -> str | None:
    if request is None:
        return None
    return getattr(request.state, "user", None)


def get_user_root_dir(username: str) -> Path:
    return USER_SPACES_DIR / _slug_username(username)


def get_user_workspaces_dir(username: str) -> Path:
    return get_user_root_dir(username) / "workspaces"


def _get_state_path(username: str) -> Path:
    return get_user_root_dir(username) / "state.json"


def load_user_state(username: str) -> dict:
    state_path = _get_state_path(username)
    if not state_path.is_file():
        return {}
    try:
        return json.loads(state_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_user_state(username: str, state: dict) -> None:
    state_path = _get_state_path(username)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        "utf-8",
    )


def get_owned_agent_ids(config, username: str | None) -> list[str]:
    if not username:
        return list(config.agents.profiles.keys())
    owned = []
    for agent_id, agent_ref in config.agents.profiles.items():
        if getattr(agent_ref, "owner_username", None) == username:
            owned.append(agent_id)
    return owned


def is_agent_owned_by_user(config, username: str | None, agent_id: str) -> bool:
    if not username:
        return agent_id in config.agents.profiles
    agent_ref = config.agents.profiles.get(agent_id)
    if agent_ref is None:
        return False
    return getattr(agent_ref, "owner_username", None) == username


def get_user_agent_order(config, username: str | None) -> list[str]:
    owned_ids = get_owned_agent_ids(config, username)
    if not username:
        ordered_ids: list[str] = []
        for agent_id in config.agents.agent_order:
            if agent_id in config.agents.profiles and agent_id not in ordered_ids:
                ordered_ids.append(agent_id)
        for agent_id in owned_ids:
            if agent_id not in ordered_ids:
                ordered_ids.append(agent_id)
        return ordered_ids

    state = load_user_state(username)
    ordered_ids: list[str] = []
    for agent_id in state.get("agent_order", []):
        if agent_id in owned_ids and agent_id not in ordered_ids:
            ordered_ids.append(agent_id)
    default_agent_id = state.get("default_agent_id")
    if (
        default_agent_id
        and default_agent_id in owned_ids
        and default_agent_id not in ordered_ids
    ):
        ordered_ids.insert(0, default_agent_id)
    for agent_id in owned_ids:
        if agent_id not in ordered_ids:
            ordered_ids.append(agent_id)
    return ordered_ids


def set_user_agent_order(username: str, agent_ids: list[str]) -> None:
    state = load_user_state(username)
    state["agent_order"] = list(agent_ids)
    save_user_state(username, state)


def get_user_default_agent_id(config, username: str | None) -> str | None:
    if not username:
        if config.agents.active_agent in config.agents.profiles:
            return config.agents.active_agent
        return None

    state = load_user_state(username)
    default_agent_id = state.get("default_agent_id")
    if default_agent_id and is_agent_owned_by_user(config, username, default_agent_id):
        return default_agent_id

    ordered = get_user_agent_order(config, username)
    if not ordered:
        return None

    state["default_agent_id"] = ordered[0]
    save_user_state(username, state)
    return ordered[0]


def set_user_default_agent_id(username: str, agent_id: str | None) -> None:
    state = load_user_state(username)
    if agent_id:
        state["default_agent_id"] = agent_id
    else:
        state.pop("default_agent_id", None)
    save_user_state(username, state)


def remove_user_agent_reference(username: str, agent_id: str) -> None:
    state = load_user_state(username)
    if state.get("default_agent_id") == agent_id:
        state.pop("default_agent_id", None)
    order = state.get("agent_order", [])
    if isinstance(order, list):
        state["agent_order"] = [item for item in order if item != agent_id]
    save_user_state(username, state)


def resolve_requested_agent_id(
    config,
    username: str | None,
    requested_agent_id: str | None,
) -> str | None:
    if not username:
        if requested_agent_id:
            return requested_agent_id
        if config.agents.active_agent in config.agents.profiles:
            return config.agents.active_agent
        return next(iter(config.agents.profiles.keys()), None)

    if requested_agent_id and requested_agent_id != "default":
        return requested_agent_id
    return get_user_default_agent_id(config, username)


def require_owned_agent_id(
    config,
    username: str | None,
    requested_agent_id: str | None,
) -> str:
    agent_id = resolve_requested_agent_id(config, username, requested_agent_id)
    if not agent_id or agent_id not in config.agents.profiles:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not is_agent_owned_by_user(config, username, agent_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent_id


def normalize_user_workspace_dir(
    username: str | None,
    requested_workspace_dir: str | None,
    agent_id: str,
) -> Path:
    if not username:
        return Path(
            requested_workspace_dir or f"{WORKING_DIR}/workspaces/{agent_id}",
        ).expanduser()

    workspace_root = get_user_workspaces_dir(username).expanduser().resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    if not requested_workspace_dir:
        return (workspace_root / agent_id).resolve()

    requested_path = Path(requested_workspace_dir).expanduser().resolve()
    if requested_path == workspace_root or requested_path.is_relative_to(
        workspace_root,
    ):
        return requested_path

    raise HTTPException(
        status_code=400,
        detail=(
            "Workspace path must stay under the current user's workspace root: "
            f"{workspace_root}"
        ),
    )


def assign_legacy_agents_to_user(username: str) -> None:
    config = load_config()
    changed = False
    for agent_ref in config.agents.profiles.values():
        if getattr(agent_ref, "owner_username", None):
            continue
        agent_ref.owner_username = username
        changed = True
    if changed:
        save_config(config)


def migrate_username(old_username: str, new_username: str) -> None:
    if old_username == new_username:
        return

    config = load_config()
    changed = False
    for agent_ref in config.agents.profiles.values():
        if getattr(agent_ref, "owner_username", None) == old_username:
            agent_ref.owner_username = new_username
            changed = True
    if changed:
        save_config(config)

    old_root = get_user_root_dir(old_username)
    new_root = get_user_root_dir(new_username)
    if old_root.exists() and not new_root.exists():
        try:
            new_root.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_root), str(new_root))
        except OSError:
            logger.warning(
                "Failed to move user state directory from %s to %s",
                old_root,
                new_root,
            )


def _ensure_heartbeat_file(workspace_dir: Path, language: str) -> None:
    heartbeat_file = workspace_dir / "HEARTBEAT.md"
    if heartbeat_file.exists():
        return

    default_heartbeat_mds = {
        "zh": """# Heartbeat checklist
- 扫描收件箱紧急邮件
- 查看未来 2h 的日历
- 检查待办是否卡住
- 若安静超过 8h，轻量 check-in
""",
        "en": """# Heartbeat checklist
- Scan inbox for urgent email
- Check calendar for next 2h
- Check tasks for blockers
- Light check-in if quiet for 8h
""",
        "ru": """# Heartbeat checklist
- Проверить входящие на срочные письма
- Просмотреть календарь на ближайшие 2 часа
- Проверить задачи на наличие блокировок
- Лёгкая проверка при отсутствии активности более 8 часов
""",
    }
    heartbeat_file.write_text(
        default_heartbeat_mds.get(language, default_heartbeat_mds["en"]).strip(),
        "utf-8",
    )


def _initialize_user_workspace(workspace_dir: Path, language: str) -> None:
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "sessions").mkdir(exist_ok=True)
    (workspace_dir / "memory").mkdir(exist_ok=True)
    get_workspace_skills_dir(workspace_dir).mkdir(exist_ok=True)
    copy_workspace_md_files(language, workspace_dir, md_template_id=None)
    _ensure_heartbeat_file(workspace_dir, language)
    jobs_path = workspace_dir / "jobs.json"
    if not jobs_path.exists():
        jobs_path.write_text(
            json.dumps(
                {"version": 1, "jobs": []},
                ensure_ascii=False,
                indent=2,
            ),
            "utf-8",
        )
    chats_path = workspace_dir / "chats.json"
    if not chats_path.exists():
        chats_path.write_text(
            json.dumps(
                {"version": 1, "chats": []},
                ensure_ascii=False,
                indent=2,
            ),
            "utf-8",
        )


def ensure_user_default_agent(username: str | None) -> str | None:
    if not username:
        return None

    config = load_config()
    existing = get_user_default_agent_id(config, username)
    if existing:
        return existing

    owned_ids = get_owned_agent_ids(config, username)
    if owned_ids:
        set_user_default_agent_id(username, owned_ids[0])
        return owned_ids[0]

    slug = _slug_username(username)
    candidate = f"user_{slug}_default"
    suffix = 1
    while candidate in config.agents.profiles:
        suffix += 1
        candidate = f"user_{slug}_default_{suffix}"

    language = normalize_agent_language(config.agents.language or "zh")
    workspace_dir = (get_user_workspaces_dir(username) / "default").resolve()
    _initialize_user_workspace(workspace_dir, language)

    agent_config = AgentProfileConfig(
        id=candidate,
        name=f"{username} Default Agent",
        description=f"Private workspace for {username}",
        workspace_dir=str(workspace_dir),
        language=language,
        channels=ChannelConfig(),
        mcp=MCPConfig(),
        heartbeat=HeartbeatConfig(),
        tools=ToolsConfig(),
    )
    agent_ref = AgentProfileRef(
        id=candidate,
        workspace_dir=str(workspace_dir),
        enabled=True,
        owner_username=username,
    )
    config.agents.profiles[candidate] = agent_ref
    if candidate not in config.agents.agent_order:
        config.agents.agent_order.append(candidate)
    save_config(config)
    save_agent_config(candidate, agent_config)

    set_user_default_agent_id(username, candidate)
    set_user_agent_order(username, [candidate])
    logger.info("Created default private agent %s for user %s", candidate, username)
    return candidate
