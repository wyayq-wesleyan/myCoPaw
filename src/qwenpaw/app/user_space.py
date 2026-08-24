"""User ownership and workspace isolation for offline multi-user deployments."""
from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

from fastapi import HTTPException, Request

from ..agents.skill_system import (
    SkillService,
    install_initial_skill,
    list_initial_skill_names,
)
from ..agents.templates import DEFAULT_AGENT_TEMPLATE, build_agent_template
from ..agents.utils import copy_workspace_md_files, normalize_agent_language
from ..config.config import (
    AgentProfileRef,
    ToolsConfig,
    load_agent_config,
    save_agent_config,
)
from ..config.utils import load_config, save_config
from ..constant import WORKING_DIR

logger = logging.getLogger(__name__)
USER_STATE_ROOT = WORKING_DIR / "users"
USER_WORKSPACE_ROOT = WORKING_DIR / "workspaces"
MODEL_PERMISSIONS = "model_permissions"
DISABLED_SKILLS = "disabled_skills"
DISABLED_TOOLS = "disabled_tools"
MENU_PERMISSIONS = "menu_permissions"
ACCOUNT_SKILLS = "account_skills"
ACCOUNT_TOOLS = "account_tools"
RUNNING_CONFIG_OVERRIDE = "running_config_override"

# Regular accounts start with the focused, day-to-day console.  The chat
# entry and agent selector are rendered outside the registry menu, therefore
# they do not need an explicit entry here.
DEFAULT_USER_MENU_IDS = frozenset(
    {
        "core.user-management",
        "core.agents",
        "core.files",
        "core.knowledge",
        "core.sessions",
        "core.cron-jobs",
        "core.token-usage",
        "core.skills",
        "core.tools",
        "core.inbox",
    },
)


def _slug(username: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", username.strip()).strip(".-")
    return value.lower() or "user"


def get_request_username(request: Request | None) -> str | None:
    if request is None:
        return None
    username = getattr(request.state, "user", None)
    if username:
        return username
    # Requests from an explicitly allow-listed local host can bypass the auth
    # middleware. Still recover the identity from the bearer token so account
    # scoped permissions do not silently fall back to the unrestricted view.
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        try:
            from .auth import verify_token

            return verify_token(authorization[7:])
        except Exception:  # noqa: BLE001
            logger.warning("Could not resolve the request bearer token", exc_info=True)
    return None


def get_user_root(username: str) -> Path:
    """Return the filesystem root owned by one account.

    The administrator keeps the historical default workspace path. Regular
    accounts get one top-level directory named after the normalized username.
    """
    return USER_WORKSPACE_ROOT / ("default" if _slug(username) == "admin" else _slug(username))


def get_user_state_root(username: str) -> Path:
    """Return the private metadata directory, outside the visible workspace."""
    return USER_STATE_ROOT / _slug(username)


def is_admin_user(username: str | None) -> bool:
    if not username:
        return False
    try:
        from .auth import get_user_info

        user = get_user_info(username)
        return bool(user and user.get("role") == "admin")
    except Exception:  # noqa: BLE001
        return _slug(username) == "admin"


def _state_path(username: str) -> Path:
    return get_user_state_root(username) / "state.json"


def _legacy_state_path(username: str) -> Path:
    return USER_STATE_ROOT / _slug(username) / "workspaces" / "default" / "state.json"


def load_user_state(username: str) -> dict:
    path = _state_path(username)
    if not path.is_file():
        # Read the old location during the one-time workspace migration.
        path = _legacy_state_path(username)
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


def _account_agent_workspace(username: str, agent_id: str) -> Path:
    """Return the canonical workspace path for an account-owned agent."""
    state = load_user_state(username)
    default_id = state.get("default_agent_id")
    if agent_id == default_id or agent_id.endswith("_default"):
        return get_user_root(username)
    return get_user_root(username) / "agents" / agent_id


def _ensure_account_directories(username: str) -> None:
    """Create the account-level file categories inside its isolated root."""
    root = get_user_root(username)
    for name in ("archives", "diary", "knowledge"):
        (root / name).mkdir(parents=True, exist_ok=True)


def _migrate_user_workspace_paths(username: str) -> None:
    """Move legacy account workspaces into the account-isolated layout."""
    if is_admin_user(username):
        return
    config = load_config()
    state = load_user_state(username)
    default_id = state.get("default_agent_id")
    changed = False
    legacy_root = USER_STATE_ROOT / _slug(username)
    account_root = get_user_root(username).resolve()
    for agent_id, profile in config.agents.profiles.items():
        if getattr(profile, "owner_username", None) != username:
            continue
        current = Path(profile.workspace_dir).expanduser().resolve()
        if current == account_root or account_root in current.parents:
            continue
        target = _account_agent_workspace(username, agent_id).resolve()
        if default_id == agent_id:
            target = account_root
        target.parent.mkdir(parents=True, exist_ok=True)
        if current.exists():
            if target.exists():
                shutil.copytree(current, target, dirs_exist_ok=True)
                shutil.rmtree(current, ignore_errors=True)
            else:
                shutil.move(str(current), str(target))
        profile.workspace_dir = str(target)
        changed = True
    if changed:
        save_config(config)
    # Keep account metadata outside the file tree exposed to the user.
    if legacy_root.exists() and legacy_root.resolve() != account_root:
        legacy_state = legacy_root / "state.json"
        if legacy_state.is_file() and not _state_path(username).is_file():
            _state_path(username).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_state, _state_path(username))


def _names(values: list[str] | None) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values or [] if str(value).strip()))


def _skill_pool_names() -> list[str]:
    """Compatibility name for the offline initial skill catalog."""
    try:
        return _names(list_initial_skill_names())
    except Exception:  # noqa: BLE001
        logger.warning("Unable to read offline initial skills", exc_info=True)
        return []


def _tool_pool() -> dict:
    try:
        return ToolsConfig().builtin_tools
    except Exception:  # noqa: BLE001
        logger.warning("Unable to read the local tool pool", exc_info=True)
        return {}


def get_account_skills(username: str) -> list[str]:
    state = load_user_state(username)
    pool = _skill_pool_names()
    if ACCOUNT_SKILLS in state:
        selected = set(_names(state.get(ACCOUNT_SKILLS)))
        return [name for name in pool if name in selected]
    disabled = set(get_disabled_names(username, DISABLED_SKILLS))
    return [name for name in pool if name not in disabled]


def get_account_tools(username: str) -> list[str]:
    state = load_user_state(username)
    pool = list(_tool_pool())
    if ACCOUNT_TOOLS in state:
        selected = set(_names(state.get(ACCOUNT_TOOLS)))
        return [name for name in pool if name in selected]
    disabled = set(get_disabled_names(username, DISABLED_TOOLS))
    return [name for name in pool if name not in disabled]


def set_account_skills(username: str, skill_names: list[str] | None) -> None:
    pool = _skill_pool_names()
    selected = set(_names(skill_names))
    unknown = selected - set(pool)
    if unknown:
        raise ValueError(f"Unknown skill(s): {', '.join(sorted(unknown))}")
    state = load_user_state(username)
    state[ACCOUNT_SKILLS] = [name for name in pool if name in selected]
    state.pop(DISABLED_SKILLS, None)
    save_user_state(username, state)

    for _agent_id, profile in _owned_agent_profiles(username):
        workspace = Path(profile.workspace_dir)
        for name in pool:
            if name in selected:
                result = install_initial_skill(name=name, workspace_dir=workspace)
                if not result.get("success") and result.get("reason") not in {
                    "already_exists",
                    "conflict",
                }:
                    logger.warning("Could not assign skill %s to %s: %s", name, workspace, result)
                try:
                    SkillService(workspace).enable_skill(name)
                except Exception:  # noqa: BLE001
                    logger.warning("Could not enable skill %s in %s", name, workspace, exc_info=True)
            else:
                SkillService(workspace).disable_skill(name)


def sync_admin_initial_skills() -> int:
    """Migrate the legacy local skill pool into admin workspaces once.

    The pool UI is gone, but existing deployments still keep its prepared
    skill files under ``WORKING_DIR/skill_pool``. Copy them into every admin
    workspace so the admin account starts with the same default skills. A
    marker prevents later restarts from undoing intentional admin changes.
    """
    marker = Path(WORKING_DIR) / ".offline_initial_skills_migrated"
    if marker.exists():
        return 0

    names = list_initial_skill_names()
    migrated = 0
    for _agent_id, profile in _owned_agent_profiles("admin"):
        workspace = Path(profile.workspace_dir)
        for name in names:
            result = install_initial_skill(
                skill_name=name,
                workspace_dir=workspace,
                overwrite=False,
            )
            if result.get("success"):
                migrated += 1
            else:
                logger.warning(
                    "Could not migrate initial skill %s to admin workspace %s: %s",
                    name,
                    workspace,
                    result,
                )

    if migrated or not names:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("offline initial skills migrated\n", encoding="utf-8")
    return migrated


def set_account_tools(username: str, tool_names: list[str] | None) -> None:
    catalog = _tool_pool()
    selected = set(_names(tool_names))
    unknown = selected - set(catalog)
    if unknown:
        raise ValueError(f"Unknown tool(s): {', '.join(sorted(unknown))}")
    state = load_user_state(username)
    state[ACCOUNT_TOOLS] = [name for name in catalog if name in selected]
    state.pop(DISABLED_TOOLS, None)
    save_user_state(username, state)

    for agent_id, _profile in _owned_agent_profiles(username):
        try:
            config = load_agent_config(agent_id)
            if config.tools is None:
                config.tools = ToolsConfig()
            for name, template in catalog.items():
                if name not in config.tools.builtin_tools:
                    config.tools.builtin_tools[name] = template.model_copy(deep=True)
                config.tools.builtin_tools[name].enabled = name in selected
            save_agent_config(agent_id, config)
        except Exception:  # noqa: BLE001
            logger.warning("Could not apply account tools to %s", agent_id, exc_info=True)


def get_model_permissions(username: str) -> list[dict[str, str]] | None:
    state = load_user_state(username)
    if MODEL_PERMISSIONS not in state:
        return None
    values = state.get(MODEL_PERMISSIONS, [])
    if not isinstance(values, list):
        return []
    return [
        {"provider_id": str(item["provider_id"]), "model_id": str(item["model_id"])}
        for item in values
        if isinstance(item, dict) and item.get("provider_id") and item.get("model_id")
    ]


def set_model_permissions(username: str, permissions: list[dict[str, str]] | None) -> None:
    state = load_user_state(username)
    if permissions is None:
        state.pop(MODEL_PERMISSIONS, None)
    else:
        state[MODEL_PERMISSIONS] = [
            {"provider_id": str(item["provider_id"]), "model_id": str(item["model_id"])}
            for item in permissions
            if item.get("provider_id") and item.get("model_id")
        ]
    save_user_state(username, state)


def get_disabled_names(username: str, key: str) -> list[str]:
    state = load_user_state(username)
    if key == DISABLED_SKILLS and ACCOUNT_SKILLS in state:
        selected = set(_names(state.get(ACCOUNT_SKILLS)))
        return [name for name in _skill_pool_names() if name not in selected]
    if key == DISABLED_TOOLS and ACCOUNT_TOOLS in state:
        selected = set(_names(state.get(ACCOUNT_TOOLS)))
        return [name for name in _tool_pool() if name not in selected]
    value = state.get(key, [])
    return _names(value if isinstance(value, list) else [])


def set_disabled_names(username: str, key: str, values: list[str] | None) -> None:
    state = load_user_state(username)
    previous = _names(
        state.get(key, []) if isinstance(state.get(key, []), list) else [],
    )
    state[key] = _names(values)
    save_user_state(username, state)
    if key == DISABLED_TOOLS:
        _apply_tool_permissions(username, previous, state[key])
    elif key == DISABLED_SKILLS:
        _apply_skill_permissions(username, previous, state[key])


def _owned_agent_profiles(username: str):
    config = load_config()
    return [
        (agent_id, profile)
        for agent_id, profile in config.agents.profiles.items()
        if getattr(profile, "owner_username", None) == username
    ]


def _apply_tool_permissions(
    username: str,
    previously_disabled: list[str],
    disabled: list[str],
) -> None:
    """Apply account tool permissions to all of the user's agent configs."""
    from ..config.config import load_agent_config, save_agent_config

    previous = set(previously_disabled)
    blocked = set(disabled)
    for agent_id, _profile in _owned_agent_profiles(username):
        try:
            agent_config = load_agent_config(agent_id)
            tools = getattr(getattr(agent_config, "tools", None), "builtin_tools", {})
            changed = False
            for name in blocked:
                if name in tools and tools[name].enabled:
                    tools[name].enabled = False
                    changed = True
            for name in previous - blocked:
                if name in tools and not tools[name].enabled:
                    tools[name].enabled = True
                    changed = True
            if changed:
                save_agent_config(agent_id, agent_config)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not apply tool permissions to %s: %s", agent_id, exc)


def _apply_skill_permissions(
    username: str,
    previously_disabled: list[str],
    disabled: list[str],
) -> None:
    """Enable or disable workspace skills so the runtime sees the same set."""
    from ..agents.skill_system import SkillService

    previous = set(previously_disabled)
    blocked = set(disabled)
    for _agent_id, profile in _owned_agent_profiles(username):
        try:
            service = SkillService(Path(profile.workspace_dir))
            for name in blocked:
                try:
                    service.disable_skill(name)
                except (FileNotFoundError, ValueError):
                    continue
            for name in previous - blocked:
                try:
                    service.enable_skill(name)
                except (FileNotFoundError, ValueError):
                    continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not apply skill permissions for %s: %s", username, exc)


def get_menu_permissions(username: str, role: str = "user") -> list[str] | None:
    """Return visible console menu ids for a user.

    ``None`` deliberately means unrestricted and is reserved for an
    administrator.  Keeping the default allow-list here, instead of only in
    the frontend, makes newly created accounts safe even before their first
    settings update.
    """
    if role == "admin":
        return None
    state = load_user_state(username)
    if MENU_PERMISSIONS not in state:
        return sorted(DEFAULT_USER_MENU_IDS)
    values = state.get(MENU_PERMISSIONS, [])
    # Resource sharing approvals are delivered through the local console
    # inbox. Keep it available to existing accounts as well as new ones.
    return sorted(
        set(_names(values if isinstance(values, list) else []))
        | {"core.skills", "core.tools", "core.inbox", "core.knowledge"}
    )


def set_menu_permissions(username: str, permissions: list[str] | None) -> None:
    state = load_user_state(username)
    if permissions is None:
        state.pop(MENU_PERMISSIONS, None)
    else:
        # Knowledge space is part of the standard console for every account.
        # Keep it enabled even when an older admin form submits a stale list.
        state[MENU_PERMISSIONS] = _names([*_names(permissions), "core.knowledge"])
    save_user_state(username, state)


def has_running_config_override(username: str) -> bool:
    """Return whether an administrator saved a dedicated runtime policy."""
    return bool(load_user_state(username).get(RUNNING_CONFIG_OVERRIDE, False))


def set_running_config_override(username: str, enabled: bool) -> None:
    """Toggle a user's dedicated runtime policy marker."""
    state = load_user_state(username)
    if enabled:
        state[RUNNING_CONFIG_OVERRIDE] = True
    else:
        state.pop(RUNNING_CONFIG_OVERRIDE, None)
    save_user_state(username, state)


def is_model_allowed(username: str | None, provider_id: str, model_id: str) -> bool:
    if not username:
        return True
    permissions = get_model_permissions(username)
    if permissions is None:
        # Provider credentials and model access are managed centrally.  An
        # account without an explicit entitlement must not inherit every
        # configured model merely because it was created before this setting
        # existed.  The administrator remains unrestricted.
        try:
            from .auth import get_user_info

            user = get_user_info(username)
            return bool(user and user.get("role") == "admin")
        except Exception:  # noqa: BLE001
            logger.warning("Could not determine model access for %s", username)
            return False
    return any(
        item["provider_id"] == provider_id and item["model_id"] == model_id
        for item in permissions
    )


def user_agent_ids(config, username: str | None) -> list[str]:
    if not username or is_admin_user(username):
        return list(config.agents.profiles)
    _migrate_user_workspace_paths(username)
    config = load_config()
    owned = [
        agent_id
        for agent_id, profile in config.agents.profiles.items()
        if getattr(profile, "owner_username", None) == username
    ]
    saved_order = load_user_state(username).get("agent_order", [])
    ordered = [agent_id for agent_id in saved_order if agent_id in owned]
    return ordered + [agent_id for agent_id in owned if agent_id not in ordered]


def set_user_agent_order(username: str, agent_ids: list[str]) -> None:
    """Persist a user's ordering without changing other users' agents."""
    config = load_config()
    owned = user_agent_ids(config, username)
    if set(agent_ids) != set(owned) or len(agent_ids) != len(set(agent_ids)):
        raise ValueError("Each owned agent ID must appear exactly once.")
    state = load_user_state(username)
    state["agent_order"] = list(agent_ids)
    save_user_state(username, state)


def is_agent_owned(config, username: str | None, agent_id: str) -> bool:
    if not username:
        return agent_id in config.agents.profiles
    if is_admin_user(username):
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
    _migrate_user_workspace_paths(username)
    config = load_config()
    existing = default_agent_id(config, username)
    if existing:
        _ensure_account_directories(username)
        return existing

    agent_id = f"user_{_slug(username)}_default"
    suffix = 1
    while agent_id in config.agents.profiles:
        suffix += 1
        agent_id = f"user_{_slug(username)}_default_{suffix}"

    workspace = get_user_root(username).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    _ensure_account_directories(username)
    language = normalize_agent_language(config.agents.language or "zh")
    template = build_agent_template(
        DEFAULT_AGENT_TEMPLATE,
        agent_id=agent_id,
        workspace_dir=workspace,
        fallback_language=language,
        name=f"{username} 的智能体",
        description="用户独立工作区",
    )
    # Running configuration is deployment-wide in the offline product.  A
    # new account still owns its workspace and chats, but inherits the current
    # administrator settings for model context, memory, and execution.
    admin_agent: str | None = None
    try:
        from .auth import list_users
        from ..config.config import load_agent_config

        admin_names = {
            str(item["username"])
            for item in list_users()
            if item.get("role") == "admin"
        }
        admin_agent = next(
            (
                agent_id
                for agent_id, profile in config.agents.profiles.items()
                if getattr(profile, "owner_username", None) in admin_names
            ),
            None,
        )
        if admin_agent:
            admin_config = load_agent_config(admin_agent)
            template.agent_config.running = admin_config.running.model_copy(
                deep=True,
            )
            template.agent_config.approval_level = "OFF"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not inherit admin running config: %s", exc)
    copy_workspace_md_files(
        language,
        workspace,
        md_template_id=template.md_template_id,
    )
    if admin_agent:
        try:
            admin_workspace = Path(
                config.agents.profiles[admin_agent].workspace_dir,
            )
            source_skills = admin_workspace / "skills"
            if source_skills.is_dir():
                shutil.copytree(source_skills, workspace / "skills", dirs_exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not copy administrator skills: %s", exc)
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
    state = load_user_state(username)
    state["default_agent_id"] = agent_id
    state["agent_order"] = [agent_id]
    save_user_state(username, state)
    logger.info("Created isolated default agent %s for %s", agent_id, username)
    return agent_id


def migrate_all_user_workspaces() -> None:
    """Apply the account workspace layout to every registered account."""
    try:
        from .auth import list_users

        usernames = [str(item["username"]) for item in list_users()]
    except Exception:  # noqa: BLE001
        logger.warning("Could not enumerate users for workspace migration", exc_info=True)
        return
    for username in usernames:
        try:
            ensure_user_default_agent(username)
        except Exception:  # noqa: BLE001
            logger.warning("Could not migrate workspace for %s", username, exc_info=True)


def require_owned_agent_id(config, username: str | None, requested: str | None) -> str:
    if username:
        ensure_user_default_agent(username)
        config = load_config()
    # Older browser state and the initial frontend render use the literal
    # ``default`` agent id.  That id belongs to the administrator in a
    # multi-user deployment, so resolve it to the current user's own default
    # agent before applying the ownership check.
    if username and requested in (None, "", "default"):
        agent_id = default_agent_id(config, username)
    else:
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
    shutil.rmtree(USER_STATE_ROOT / _slug(username), ignore_errors=True)
