# -*- coding: utf-8 -*-
# pylint: disable=too-many-nested-blocks,too-many-branches
"""API routes for built-in tools management."""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, List, Optional

from fastapi import APIRouter, Body, HTTPException, Path, Request
from pydantic import BaseModel, Field

from ...config import load_config, save_config
from ...config.config import ToolsConfig
from ..utils import schedule_agent_reload
from ..user_resources import (
    grant_resource,
    list_resources_for_user_sync,
    register_resource,
)
from ..auth import get_user_info, list_users
from ..user_space import get_request_username

router = APIRouter(prefix="/tools", tags=["tools"])
logger = logging.getLogger(__name__)


class ToolConfigFieldType(str, Enum):
    """Tool configuration field types."""

    TEXT = "text"
    PASSWORD = "password"
    NUMBER = "number"
    BOOLEAN = "boolean"
    SELECT = "select"
    TEXTAREA = "textarea"


class ToolConfigField(BaseModel):
    """Tool configuration field definition."""

    name: str = Field(..., description="Field name")
    label: str = Field(..., description="Display label")
    type: ToolConfigFieldType = Field(
        ...,
        description="Field type",
    )
    required: bool = Field(
        default=False,
        description="Whether field is required",
    )
    placeholder: Optional[str] = Field(None, description="Placeholder text")
    help: Optional[str] = Field(None, description="Help text")
    options: Optional[List[str]] = Field(
        None,
        description="Options for select type",
    )
    default: Optional[Any] = Field(None, description="Default value")
    min: Optional[float] = Field(None, description="Minimum value for number")
    max: Optional[float] = Field(None, description="Maximum value for number")


class ToolInfo(BaseModel):
    """Tool information for API responses."""

    name: str = Field(..., description="Tool function name")
    enabled: bool = Field(..., description="Whether the tool is enabled")
    description: str = Field(default="", description="Tool description")
    async_execution: bool = Field(
        default=False,
        description="Whether to execute the tool asynchronously in background",
    )
    icon: str = Field(default="🔧", description="Emoji icon for the tool")
    requires_config: bool = Field(
        default=False,
        description="Whether tool requires configuration",
    )
    config_fields: Optional[list[ToolConfigField]] = Field(
        None,
        description="Configuration field definitions",
    )
    config_values: Optional[dict[str, Any]] = Field(
        None,
        description="Current configuration values (sensitive fields masked)",
    )
    resource_id: str = ""
    creator_username: str = ""
    source_label: str = ""
    can_share: bool = False
    custom: bool = False


class CreateCustomToolRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    url: str = Field(..., min_length=1, max_length=2000)
    method: str = Field(default="POST", max_length=10)
    headers: dict[str, str] = Field(default_factory=dict)


class ShareCustomToolRequest(BaseModel):
    target_usernames: list[str] = Field(default_factory=list)


class ToolConfigUpdate(BaseModel):
    """Tool configuration update request."""

    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Tool configuration key-value pairs",
    )


def _persist_browser_experimental(config: dict[str, Any]) -> None:
    """Persist Browser-card gating before the next process registration."""
    experimental = config.get("experimental")
    if not isinstance(experimental, bool):
        return
    application_config = load_config()
    application_config.browser.experimental = experimental
    save_config(application_config)
    if experimental:
        # Chromium must be packaged in the offline base image.  Never attempt
        # a runtime download from an intranet deployment.
        logger.info("Browser enabled; using packaged offline runtime")


def _build_tool_info(tool_config: Any, tool_name: str) -> ToolInfo:
    """Build a complete ToolInfo from a tool config, including plugin metadata.

    Reads requires_config, config_fields and config_values from the plugin
    manifest so that every endpoint returns a consistent, complete response.

    Args:
        tool_config: BuiltinToolConfig instance
        tool_name: Tool function name

    Returns:
        Fully populated ToolInfo
    """
    from ...plugins.registry import PluginRegistry

    tool_info = ToolInfo(
        name=tool_config.name,
        enabled=tool_config.enabled,
        description=tool_config.description,
        async_execution=tool_config.async_execution,
        icon=tool_config.icon or "",
    )

    registry = PluginRegistry()
    plugin_id = registry.get_plugin_id_for_tool(tool_name)
    manifest = registry.get_plugin_manifest(plugin_id) if plugin_id else None

    if manifest and "meta" in manifest:
        meta = manifest["meta"]

        config_fields_data = None
        requires_config = False

        for t in meta.get("tools", []):
            if isinstance(t, dict) and t.get("name") == tool_name:
                requires_config = t.get("requires_config", False)
                config_fields_data = t.get("config_fields", [])
                break

        if config_fields_data is None:
            requires_config = meta.get("requires_config", False)
            config_fields_data = meta.get("config_fields", [])

        tool_info.requires_config = requires_config

        if config_fields_data:
            tool_info.config_fields = [
                ToolConfigField(**field) for field in config_fields_data
            ]

        if tool_config.config:
            masked_config = dict(tool_config.config)
            for field in config_fields_data:
                if (
                    field.get("type") == "password"
                    and field["name"] in masked_config
                ):
                    if masked_config[field["name"]]:
                        masked_config[field["name"]] = "***"
            tool_info.config_values = masked_config

    if tool_name == "browser":
        config_values: dict[str, Any] = {
            "experimental": load_config().browser.experimental,
        }
        try:
            from ...agents.tools import browser_track_effective

            config_values["experimental_effective"] = browser_track_effective()
        except Exception:
            pass
        tool_info.config_values = config_values

    return tool_info


@router.get("", response_model=List[ToolInfo])
async def list_tools(
    request: Request,
) -> List[ToolInfo]:
    """List all built-in tools and enabled status for active agent.

    Returns:
        List of tool information
    """
    from ..agent_context import get_agent_for_request
    from ..user_space import get_disabled_names, get_request_username
    from ...config.config import load_agent_config

    workspace = await get_agent_for_request(request)
    agent_config = load_agent_config(workspace.agent_id)

    # Ensure tools config exists with defaults
    if not agent_config.tools or not agent_config.tools.builtin_tools:
        # Fallback to global config if agent config has no tools
        config = load_config()
        tools_config = config.tools if hasattr(config, "tools") else None
        if not tools_config:
            return []
        builtin_tools = tools_config.builtin_tools
    else:
        builtin_tools = agent_config.tools.builtin_tools

    disabled = set(
        get_disabled_names(get_request_username(request), "disabled_tools")
        if get_request_username(request)
        else []
    )
    result = [
        _build_tool_info(tool_config, tool_config.name)
        for tool_config in builtin_tools.values()
        if tool_config.name not in disabled
    ]
    username = get_request_username(request) or ""
    user = get_user_info(username) if username else None
    is_admin = bool(user and user.get("role") == "admin")
    result.extend(
        ToolInfo(
            name=str(record["name"]),
            enabled=True,
            description=str(record.get("description") or record.get("metadata", {}).get("description", "")),
            icon="🔗",
            resource_id=str(record["resource_id"]),
            creator_username=str(record.get("creator_username", "")),
            source_label=(
                "本人创建"
                if record.get("creator_username") == username
                else f"由 {record.get('creator_username', '')} 分享"
            ),
            can_share=record.get("creator_username") == username,
            custom=True,
        )
        for record in list_resources_for_user_sync(
            kind="tool", username=username, admin=is_admin
        )
    )
    return result


@router.post("/custom", response_model=ToolInfo)
async def create_custom_tool(
    body: CreateCustomToolRequest,
    request: Request,
) -> ToolInfo:
    username = get_request_username(request) or ""
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    method = body.method.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise HTTPException(status_code=400, detail="Unsupported HTTP method")
    if body.name in ToolsConfig().builtin_tools:
        raise HTTPException(status_code=409, detail="Tool name is reserved")
    if list_resources_for_user_sync(kind="tool", username=username, admin=True):
        if any(item.get("name") == body.name for item in list_resources_for_user_sync(kind="tool", username=username, admin=True)):
            raise HTTPException(status_code=409, detail="Tool name already exists")
    from urllib.parse import urlparse

    parsed = urlparse(body.url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Tool URL must be an HTTP(S) address")
    record = await register_resource(
        kind="tool",
        name=body.name.strip(),
        creator_username=username,
        metadata={
            "description": body.description.strip(),
            "url": body.url.strip(),
            "method": method,
            "headers": body.headers,
        },
    )
    for agent_id, profile in load_config().agents.profiles.items():
        if getattr(profile, "owner_username", None) == username:
            schedule_agent_reload(request, agent_id)
    return ToolInfo(
        name=record["name"], enabled=True, description=body.description,
        icon="🔗", resource_id=record["resource_id"],
        creator_username=username, source_label="本人创建", can_share=True, custom=True,
    )


@router.post("/custom/{resource_id}/share")
async def share_custom_tool(
    resource_id: str,
    body: ShareCustomToolRequest,
    request: Request,
) -> dict[str, Any]:
    username = get_request_username(request) or ""
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    users = {str(item["username"]): item for item in list_users()}
    targets = [
        name for name in dict.fromkeys(body.target_usernames)
        if name in users and users[name].get("role") != "admin" and name != username
    ]
    try:
        granted = await grant_resource(
            resource_id=resource_id,
            sender_username=username,
            recipient_usernames=targets,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    for target in granted:
        for agent_id, profile in load_config().agents.profiles.items():
            if getattr(profile, "owner_username", None) == target:
                schedule_agent_reload(request, agent_id)
    return {"granted": granted, "skipped": len(targets) - len(granted)}


@router.patch("/{tool_name}/toggle", response_model=ToolInfo)
async def toggle_tool(
    tool_name: str = Path(...),
    request: Request = None,
) -> ToolInfo:
    """Toggle tool enabled status for active agent.

    Args:
        tool_name: Tool function name
        request: FastAPI request

    Returns:
        Updated tool information

    Raises:
        HTTPException: If tool not found
    """
    from ..agent_context import get_agent_for_request
    from ...config.config import load_agent_config, save_agent_config

    workspace = await get_agent_for_request(request)
    agent_config = load_agent_config(workspace.agent_id)

    if (
        not agent_config.tools
        or tool_name not in agent_config.tools.builtin_tools
    ):
        raise HTTPException(
            status_code=404,
            detail=f"Tool '{tool_name}' not found",
        )

    # Toggle enabled status
    tool_config = agent_config.tools.builtin_tools[tool_name]
    tool_config.enabled = not tool_config.enabled

    # Save agent config
    save_agent_config(workspace.agent_id, agent_config)

    # Hot reload config (async, non-blocking)
    schedule_agent_reload(request, workspace.agent_id)

    return _build_tool_info(tool_config, tool_name)


@router.patch("/{tool_name}/async-execution", response_model=ToolInfo)
async def update_tool_async_execution(
    tool_name: str = Path(...),
    async_execution: bool = Body(..., embed=True),
    request: Request = None,
) -> ToolInfo:
    """Update tool async_execution setting for active agent.

    Args:
        tool_name: Tool function name
        async_execution: Whether to execute asynchronously
        request: FastAPI request

    Returns:
        Updated tool information

    Raises:
        HTTPException: If tool not found
    """
    from ..agent_context import get_agent_for_request
    from ...config.config import load_agent_config, save_agent_config

    workspace = await get_agent_for_request(request)
    agent_config = load_agent_config(workspace.agent_id)

    if (
        not agent_config.tools
        or tool_name not in agent_config.tools.builtin_tools
    ):
        raise HTTPException(
            status_code=404,
            detail=f"Tool '{tool_name}' not found",
        )

    # Update async_execution setting
    tool_config = agent_config.tools.builtin_tools[tool_name]
    tool_config.async_execution = async_execution

    # Save agent config
    save_agent_config(workspace.agent_id, agent_config)

    # Hot reload config (async, non-blocking)
    schedule_agent_reload(request, workspace.agent_id)

    return _build_tool_info(tool_config, tool_name)


@router.get("/{tool_name}/config")
async def get_tool_config(
    tool_name: str = Path(...),
    request: Request = None,
) -> dict[str, Any]:
    """Get tool configuration (sensitive fields masked).

    Args:
        tool_name: Tool function name
        request: FastAPI request

    Returns:
        Tool configuration with sensitive fields masked
    """
    from ...plugins.registry import PluginRegistry
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    registry = PluginRegistry()

    # Get tool config for this agent
    config = registry.get_tool_config(tool_name, workspace.agent_id) or {}

    # Mask sensitive fields
    plugin_id = registry.get_plugin_id_for_tool(tool_name)
    if plugin_id:
        manifest = registry.get_plugin_manifest(plugin_id)
        if manifest and "meta" in manifest:
            meta = manifest["meta"]

            # Try to get tool-specific config fields first
            config_fields = None
            tools = meta.get("tools", [])
            if isinstance(tools, list):
                for tool in tools:
                    if (
                        isinstance(tool, dict)
                        and tool.get("name") == tool_name
                    ):
                        config_fields = tool.get("config_fields", [])
                        break

            # Fallback to global config fields
            if config_fields is None:
                config_fields = meta.get("config_fields", [])

            masked_config = dict(config)
            for field in config_fields:
                if (
                    field.get("type") == "password"
                    and field["name"] in masked_config
                ):
                    if masked_config[field["name"]]:
                        masked_config[field["name"]] = "***"
            return masked_config

    return config


@router.post("/{tool_name}/config")
async def update_tool_config(
    tool_name: str = Path(...),
    body: ToolConfigUpdate = Body(...),
    request: Request = None,
) -> dict[str, str]:
    """Update tool configuration.

    Args:
        tool_name: Tool function name
        body: Configuration update
        request: FastAPI request

    Returns:
        Success response

    Raises:
        HTTPException: If update fails
    """
    from ...plugins.registry import PluginRegistry
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    registry = PluginRegistry()

    # Get plugin manifest to check for password fields
    plugin_id = registry.get_plugin_id_for_tool(tool_name)
    config_to_save = dict(body.config)

    if plugin_id:
        manifest = registry.get_plugin_manifest(plugin_id)
        if manifest and "meta" in manifest:
            meta = manifest["meta"]

            # Try to get tool-specific config fields first
            config_fields = None
            tools = meta.get("tools", [])
            if isinstance(tools, list):
                for tool in tools:
                    if (
                        isinstance(tool, dict)
                        and tool.get("name") == tool_name
                    ):
                        config_fields = tool.get("config_fields", [])
                        break

            # Fallback to global config fields
            if config_fields is None:
                config_fields = meta.get("config_fields", [])

            # Get existing config
            existing_config = (
                registry.get_tool_config(
                    tool_name,
                    workspace.agent_id,
                )
                or {}
            )

            # Preserve existing password values if user sent masked value
            for field in config_fields:
                if field.get("type") == "password":
                    field_name = field["name"]
                    new_value = config_to_save.get(field_name)

                    # If value is "***" (masked), keep existing value
                    if new_value == "***" and field_name in existing_config:
                        config_to_save[field_name] = existing_config[
                            field_name
                        ]

    # Save tool config for this agent
    try:
        registry.set_tool_config(tool_name, workspace.agent_id, config_to_save)

        if tool_name == "browser":
            _persist_browser_experimental(config_to_save)

        # Hot reload config to apply changes without full restart
        schedule_agent_reload(request, workspace.agent_id)

        return {"status": "success", "message": "Configuration updated"}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update config: {str(e)}",
        ) from e
