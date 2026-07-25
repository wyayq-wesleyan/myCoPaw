"""User ownership and workspace isolation for offline multi-user deployments."""
from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

from fastapi import HTTPException, Request

from ..agents.templates import DEFAULT_AGENT_TEMPLATE, build_agent_template
from ..agents.utils import copy_workspace_md_files, normalize_agent_language
from ..config.config import AgentProfileRef, save_agent_config
from ..config.utils import load_config, save_config
from ..constant import WORKING_DIR

logger = logging.getLogger(__name__)
USER_ROOT = WORKING_DIR / "users"


def _slug(username: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", username.strip()).strip(".-")
    return value.lower() or "user"


def get_request_username(request: Request | None) -> str | None:
    return getattr(request.state, "user", None) if request else None


def get_user_root(username: str) -> Path:
    return USER_ROOT / _slug(username)


def _state_path(username: str) -> Path:
    return get_user_root(username) / "state.json"


def load_user_state(username: str) -> dict:
    path = _state_path(username)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_user_state(username: str, state: dict) -> None:
    path = _state_path(username)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


def user_agent_ids(config, username: str | None) -> list[str]:
    if not username:
        return list(config.agents.profiles)
    return [
        agent_id
        for agent_id, profile in config.agents.profiles.items()
        if getattr(profile, "owner_username", None) == username
    ]


def is_agent_owned(config, username: str | None, agent_id: str) -> bool:
    if not username:
        return agent_id in config.agents.profiles
    profile = config.agents.profiles.get(agent_id)
    return bool(profile and getattr(profile, "owner_username", None) == username)


def default_agent_id(config, username: str | None) -> str | None:
    if not username:
        return config.agents.active_agent if config.agents.active_agent in config.agents.profiles else None
    state = load_user_state(username)
    agent_id = state.get("default_agent_id")
    if isinstance(agent_id, str) and is_agent_owned(config, username, agent_id):
        return agent_id
    owned = user_agent_ids(config, username)
    if not owned:
        return None
    state["default_agent_id"] = owned[0]
    state["agent_order"] = owned
    save_user_state(username, state)
    return owned[0]


def ensure_user_default_agent(username: str | None) -> str | None:
    if not username:
        return None
    config = load_config()
    existing = default_agent_id(config, username)
    if existing:
        return existing

    agent_id = f"user_{_slug(username)}_default"
    suffix = 1
    while agent_id in config.agents.profiles:
        suffix += 1
        agent_id = f"user_{_slug(username)}_default_{suffix}"

    workspace = (get_user_root(username) / "workspaces" / "default").resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    language = normalize_agent_language(config.agents.language or "zh")
    template = build_agent_template(
        DEFAULT_AGENT_TEMPLATE,
        agent_id=agent_id,
        workspace_dir=workspace,
        fallback_language=language,
        name=f"{username} 的智能体",
        description="用户独立工作区",
    )
    copy_workspace_md_files(
        language,
        workspace,
        md_template_id=template.md_template_id,
    )
    for name, content in (("chats.json", '{"version": 1, "chats": []}'), ("jobs.json", '{"version": 1, "jobs": []}')):
        path = workspace / name
        if not path.exists():
            path.write_text(content, "utf-8")

    config.agents.profiles[agent_id] = AgentProfileRef(
        id=agent_id,
        workspace_dir=str(workspace),
        owner_username=username,
    )
    config.agents.agent_order.append(agent_id)
    save_config(config)
    save_agent_config(agent_id, template.agent_config)
    save_user_state(username, {"default_agent_id": agent_id, "agent_order": [agent_id]})
    logger.info("Created isolated default agent %s for %s", agent_id, username)
    return agent_id


def require_owned_agent_id(config, username: str | None, requested: str | None) -> str:
    if username:
        ensure_user_default_agent(username)
        config = load_config()
    agent_id = requested or default_agent_id(config, username)
    if not agent_id or not is_agent_owned(config, username, agent_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent_id


def assign_unowned_agents_to_admin(username: str) -> None:
    """Claim legacy agents during the first v1 data import."""
    config = load_config()
    changed = False
    for profile in config.agents.profiles.values():
        if getattr(profile, "owner_username", None) is None:
            profile.owner_username = username
            changed = True
    if changed:
        save_config(config)


def delete_user_data(username: str) -> None:
    """Remove only the user's isolated state after its agents are deleted."""
    shutil.rmtree(get_user_root(username), ignore_errors=True)
