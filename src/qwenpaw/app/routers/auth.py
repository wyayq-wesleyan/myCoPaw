# -*- coding: utf-8 -*-
"""Authentication API endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ...constant import EnvVarLoader
from ...agents.skill_system import SkillPoolService
from ..auth import (
    authenticate,
    create_user_account,
    delete_user_account,
    get_user_info,
    has_registered_users,
    is_auth_enabled,
    list_users,
    require_admin_access,
    register_user,
    revoke_all_tokens,
    revoke_token,
    update_credentials,
    verify_token,
    set_user_enabled,
    set_user_password,
    resolve_client_ip,
)
from ..rate_limiter import rate_limiter
from ..utils import schedule_agent_reload
from ..user_space import (
    get_menu_permissions,
    get_model_permissions,
    set_menu_permissions,
    set_model_permissions,
    get_account_skills,
    get_account_tools,
    set_account_skills,
    set_account_tools,
    _tool_pool,
    _skill_pool_names,
)
from ..user_resources import list_resources_for_user_sync

router = APIRouter(prefix="/auth", tags=["auth"])


def _schedule_user_agent_reloads(request: Request, username: str) -> None:
    """Reload all owned agents after a skill or tool entitlement changes."""
    from ...config import load_config

    for agent_id, profile in load_config().agents.profiles.items():
        if getattr(profile, "owner_username", None) == username:
            schedule_agent_reload(request, agent_id)


class LoginRequest(BaseModel):
    username: str
    password: str
    expires_in: int | None = (
        None  # Token expiry in seconds, -1/0 for permanent
    )


class LoginResponse(BaseModel):
    token: str
    username: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    expires_in: int | None = (
        None  # Token expiry in seconds, -1/0 for permanent
    )


class AuthStatusResponse(BaseModel):
    enabled: bool
    has_users: bool


class CurrentUserResponse(BaseModel):
    username: str
    role: str
    enabled: bool
    menu_permissions: list[str] | None = None
    skills: list[str] = []
    tools: list[str] = []


class ModelPermission(BaseModel):
    provider_id: str
    model_id: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    model_permissions: list[ModelPermission] | None = None
    disabled_skills: list[str] | None = None
    disabled_tools: list[str] | None = None
    menu_permissions: list[str] | None = None
    skills: list[str] | None = None
    tools: list[str] | None = None


class UpdateUserStatusRequest(BaseModel):
    enabled: bool


class UpdateUserPasswordRequest(BaseModel):
    password: str


class UpdateModelPermissionsRequest(BaseModel):
    permissions: list[ModelPermission] | None = None


class UpdateNamePermissionsRequest(BaseModel):
    disabled: list[str] | None = None


class UpdateMenuPermissionsRequest(BaseModel):
    permissions: list[str] | None = None


class AccountResourceItem(BaseModel):
    name: str
    description: str = ""
    icon: str = ""


class UserResourcesResponse(BaseModel):
    username: str
    skills: list[str]
    tools: list[str]
    available_skills: list[AccountResourceItem]
    available_tools: list[AccountResourceItem]


class UpdateUserResourcesRequest(BaseModel):
    skills: list[str] = []
    tools: list[str] = []


def _resource_response(username: str) -> UserResourcesResponse:
    available_skills = [
        AccountResourceItem(
            name=skill.name,
            description=str(getattr(skill, "description", "") or ""),
            icon=str(getattr(skill, "emoji", "") or ""),
        )
        for skill in SkillPoolService().list_all_skills()
    ]
    available_tools = [
        AccountResourceItem(
            name=name,
            description=str(getattr(tool, "description", "") or ""),
            icon=str(getattr(tool, "icon", "") or ""),
        )
        for name, tool in _tool_pool().items()
    ]
    custom_skills = list_resources_for_user_sync(
        kind="skill", username=username, admin=True
    )
    custom_tools = list_resources_for_user_sync(
        kind="tool", username=username, admin=True
    )
    available_skills.extend(
        AccountResourceItem(
            name=str(item["name"]),
            description=f"自建技能，创建者：{item.get('creator_username', '')}",
        )
        for item in custom_skills
    )
    available_tools.extend(
        AccountResourceItem(
            name=str(item["name"]),
            description=f"自建工具，创建者：{item.get('creator_username', '')}",
        )
        for item in custom_tools
    )
    return UserResourcesResponse(
        username=username,
        skills=get_account_skills(username) + [
            str(item["name"]) for item in custom_skills
            if username == item.get("creator_username")
            or username in (item.get("recipients") or [])
        ],
        tools=get_account_tools(username) + [
            str(item["name"]) for item in custom_tools
            if username == item.get("creator_username")
            or username in (item.get("recipients") or [])
        ],
        available_skills=available_skills,
        available_tools=available_tools,
    )


def _user_response(user: dict) -> CurrentUserResponse:
    username = str(user["username"])
    custom_skills = list_resources_for_user_sync(
        kind="skill", username=username, admin=True
    )
    custom_tools = list_resources_for_user_sync(
        kind="tool", username=username, admin=True
    )
    return CurrentUserResponse(
        **user,
        skills=get_account_skills(username) + [
            str(item["name"]) for item in custom_skills
            if username == item.get("creator_username")
            or username in (item.get("recipients") or [])
        ],
        tools=get_account_tools(username) + [
            str(item["name"]) for item in custom_tools
            if username == item.get("creator_username")
            or username in (item.get("recipients") or [])
        ],
    )


@router.post("/login")
async def login(request: Request, req: LoginRequest):
    """Authenticate with username and password.

    Optional `expires_in` field:
    - Positive integer: token expires in N seconds
    - 0 or -1: permanent token (100 years)
    - None/omitted: default 7 days
    """
    if not is_auth_enabled():
        return LoginResponse(token="", username="")

    # Get client IP for rate limiting
    client_ip = resolve_client_ip(request)

    # Check if user account is locked
    if rate_limiter.is_user_locked(req.username):
        raise HTTPException(
            status_code=423,
            detail="Account temporarily locked. Please try again later",
        )

    # Check if IP is locked or rate-limited
    if rate_limiter.is_ip_locked(client_ip):
        raise HTTPException(
            status_code=423,
            detail="Too many login attempts. Please try again later",
        )

    if rate_limiter.is_ip_rate_limited(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please slow down",
        )

    # Attempt authentication
    token = authenticate(req.username, req.password, req.expires_in)
    if token is None:
        # Record failed attempt
        rate_limiter.record_login_attempt(
            client_ip,
            req.username,
            success=False,
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password",
        )

    # Record successful attempt
    rate_limiter.record_login_attempt(client_ip, req.username, success=True)

    return LoginResponse(token=token, username=req.username)


@router.post("/register")
async def register(req: RegisterRequest):
    """Register the single user account (only allowed once).

    Optional `expires_in` field:
    - Positive integer: token expires in N seconds
    - 0 or -1: permanent token (100 years)
    - None/omitted: default 7 days
    """
    env_flag = EnvVarLoader.get_str("QWENPAW_AUTH_ENABLED", "").strip().lower()
    if env_flag not in ("true", "1", "yes"):
        raise HTTPException(
            status_code=403,
            detail="Authentication is not enabled",
        )

    if has_registered_users():
        raise HTTPException(
            status_code=403,
            detail="User already registered",
        )

    if not req.username.strip() or not req.password.strip():
        raise HTTPException(
            status_code=400,
            detail="Username and password are required",
        )

    token = register_user(req.username.strip(), req.password, req.expires_in)
    if token is None:
        raise HTTPException(
            status_code=409,
            detail="Registration failed",
        )

    return LoginResponse(token=token, username=req.username.strip())


@router.get("/status")
async def auth_status():
    """Check if authentication is enabled and whether a user exists."""
    return AuthStatusResponse(
        enabled=is_auth_enabled(),
        has_users=has_registered_users(),
    )


@router.get("/verify")
async def verify(request: Request):
    """Verify that the caller's Bearer token is still valid."""
    if not is_auth_enabled():
        return {"valid": True, "username": ""}

    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail="No token provided")

    username = verify_token(token)
    if username is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
        )

    return {"valid": True, "username": username}


def _request_username(request: Request) -> str:
    token = request.headers.get("Authorization", "")
    username = verify_token(token[7:]) if token.startswith("Bearer ") else None
    if username is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return username


@router.get("/me", response_model=CurrentUserResponse)
async def get_current_user(request: Request) -> CurrentUserResponse:
    username = _request_username(request)
    user = get_user_info(username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return _user_response(user)


@router.get("/users", response_model=list[CurrentUserResponse])
async def get_users(request: Request) -> list[CurrentUserResponse]:
    require_admin_access(_request_username(request))
    return [_user_response(user) for user in list_users()]


@router.get("/users/{username}/resources", response_model=UserResourcesResponse)
async def get_user_resources(username: str, request: Request) -> UserResourcesResponse:
    require_admin_access(_request_username(request))
    if get_user_info(username) is None:
        raise HTTPException(status_code=404, detail="User not found")
    return _resource_response(username)


@router.put("/users/{username}/resources", response_model=UserResourcesResponse)
async def update_user_resources(
    username: str,
    req: UpdateUserResourcesRequest,
    request: Request,
) -> UserResourcesResponse:
    require_admin_access(_request_username(request))
    if get_user_info(username) is None:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        # Custom resources are granted through their owner's share approval.
        # This endpoint manages the built-in offline pool only.
        set_account_skills(
            username,
            [item for item in req.skills if item in _skill_pool_names()],
        )
        set_account_tools(
            username,
            [item for item in req.tools if item in _tool_pool()],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _schedule_user_agent_reloads(request, username)
    return _resource_response(username)


@router.post("/users", response_model=CurrentUserResponse, status_code=201)
async def create_user(req: CreateUserRequest, request: Request) -> CurrentUserResponse:
    acting_username = _request_username(request)
    require_admin_access(acting_username)
    try:
        user = create_user_account(req.username, req.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    from ..user_space import ensure_user_default_agent

    ensure_user_default_agent(req.username)
    set_model_permissions(
        req.username,
        [entry.model_dump() for entry in req.model_permissions]
        if req.model_permissions is not None
        else None,
    )
    # Skills and tools belong to the account. The same entitlement is applied
    # to every current and future agent owned by this user.
    selected_skills = req.skills
    if selected_skills is None:
        selected_skills = [
            name
            for name in get_account_skills(acting_username)
            if name not in set(req.disabled_skills or [])
        ]
    selected_tools = req.tools
    if selected_tools is None:
        selected_tools = [
            name
            for name in get_account_tools(acting_username)
            if name not in set(req.disabled_tools or [])
        ]
    # A newly created account receives the selected built-in resources. Custom
    # resources keep their creator/approval boundary and are never silently
    # copied into another account during account creation.
    set_account_skills(
        req.username,
        [name for name in selected_skills if name in _skill_pool_names()],
    )
    set_account_tools(
        req.username,
        [name for name in selected_tools if name in _tool_pool()],
    )
    set_menu_permissions(req.username, req.menu_permissions)
    _schedule_user_agent_reloads(request, req.username)
    user = get_user_info(req.username) or user
    return _user_response(user)


@router.patch("/users/{username}/status")
async def update_user_status(username: str, req: UpdateUserStatusRequest, request: Request) -> dict:
    require_admin_access(_request_username(request))
    try:
        set_user_enabled(username, req.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True}


@router.put("/users/{username}/password")
async def update_user_password(username: str, req: UpdateUserPasswordRequest, request: Request) -> dict:
    require_admin_access(_request_username(request))
    try:
        set_user_password(username, req.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True}


@router.delete("/users/{username}")
async def delete_user(username: str, request: Request) -> dict:
    require_admin_access(_request_username(request))
    try:
        delete_user_account(username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    from ..user_space import delete_user_data

    delete_user_data(username)
    return {"success": True}


@router.get("/users/{username}/model-permissions")
async def user_model_permissions(username: str, request: Request) -> dict:
    require_admin_access(_request_username(request))
    return {"username": username, "permissions": get_model_permissions(username)}


@router.put("/users/{username}/model-permissions")
async def update_model_permissions(
    username: str,
    req: UpdateModelPermissionsRequest,
    request: Request,
) -> dict:
    require_admin_access(_request_username(request))
    set_model_permissions(
        username,
        [entry.model_dump() for entry in req.permissions]
        if req.permissions is not None
        else None,
    )
    return await user_model_permissions(username, request)


@router.get("/users/{username}/skill-permissions")
async def user_skill_permissions(username: str, request: Request) -> dict:
    require_admin_access(_request_username(request))
    selected = set(get_account_skills(username))
    return {
        "username": username,
        "disabled": [
            skill.name
            for skill in SkillPoolService().list_all_skills()
            if skill.name not in selected
        ],
    }


@router.put("/users/{username}/skill-permissions")
async def update_skill_permissions(
    username: str,
    req: UpdateNamePermissionsRequest,
    request: Request,
) -> dict:
    require_admin_access(_request_username(request))
    blocked = set(req.disabled or [])
    set_account_skills(
        username,
        [
            skill.name
            for skill in SkillPoolService().list_all_skills()
            if skill.name not in blocked
        ],
    )
    _schedule_user_agent_reloads(request, username)
    return await user_skill_permissions(username, request)


@router.get("/users/{username}/tool-permissions")
async def user_tool_permissions(username: str, request: Request) -> dict:
    require_admin_access(_request_username(request))
    selected = set(get_account_tools(username))
    return {
        "username": username,
        "disabled": [name for name in _tool_pool() if name not in selected],
    }


@router.put("/users/{username}/tool-permissions")
async def update_tool_permissions(
    username: str,
    req: UpdateNamePermissionsRequest,
    request: Request,
) -> dict:
    require_admin_access(_request_username(request))
    blocked = set(req.disabled or [])
    set_account_tools(
        username,
        [name for name in _tool_pool() if name not in blocked],
    )
    _schedule_user_agent_reloads(request, username)
    return await user_tool_permissions(username, request)


@router.get("/users/{username}/menu-permissions")
async def user_menu_permissions(username: str, request: Request) -> dict:
    require_admin_access(_request_username(request))
    user = get_user_info(username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "username": username,
        "permissions": get_menu_permissions(username, str(user["role"])),
    }


@router.put("/users/{username}/menu-permissions")
async def update_menu_permissions(
    username: str,
    req: UpdateMenuPermissionsRequest,
    request: Request,
) -> dict:
    require_admin_access(_request_username(request))
    user = get_user_info(username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user["role"] == "admin":
        raise HTTPException(
            status_code=400,
            detail="Administrator menu permissions cannot be restricted",
        )
    set_menu_permissions(username, req.permissions)
    return await user_menu_permissions(username, request)


class UpdateProfileRequest(BaseModel):
    current_password: str
    new_username: str | None = None
    new_password: str | None = None
    expires_in: int | None = (
        None  # Token expiry in seconds, -1/0 for permanent
    )


@router.post("/update-profile")
async def update_profile(req: UpdateProfileRequest, request: Request):
    """Update username and/or password for the authenticated user."""
    if not is_auth_enabled():
        raise HTTPException(
            status_code=403,
            detail="Authentication is not enabled",
        )

    if not has_registered_users():
        raise HTTPException(
            status_code=403,
            detail="No user registered",
        )

    # Verify caller is authenticated
    auth_header = request.headers.get("Authorization", "")
    caller_token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not caller_token or verify_token(caller_token) is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if not req.new_username and not req.new_password:
        raise HTTPException(
            status_code=400,
            detail="Nothing to update",
        )

    if req.new_username is not None and not req.new_username.strip():
        raise HTTPException(
            status_code=400,
            detail="Username cannot be empty",
        )

    if req.new_password is not None and not req.new_password.strip():
        raise HTTPException(
            status_code=400,
            detail="Password cannot be empty",
        )

    token = update_credentials(
        current_password=req.current_password,
        new_username=req.new_username,
        new_password=req.new_password,
        expiry_seconds=req.expires_in,
    )
    if token is None:
        raise HTTPException(
            status_code=401,
            detail="Current password is incorrect",
        )

    username = req.new_username.strip() if req.new_username else ""
    return LoginResponse(token=token, username=username)


class RevokeTokenRequest(BaseModel):
    token: str | None = (
        None  # Optional: revoke specific token, or current if omitted
    )


@router.post("/revoke-token")
async def revoke_single_token(req: RevokeTokenRequest, request: Request):
    """Revoke a single token by adding it to the blacklist.

    If `token` is provided in the request body, revokes that token.
    If `token` is omitted, revokes the token used for authentication
    (current token).

    This allows you to:
    - Revoke a leaked token from another device
    - Logout from the current session
    """
    if not is_auth_enabled():
        raise HTTPException(
            status_code=403,
            detail="Authentication is not enabled",
        )

    # Get current token for authentication
    auth_header = request.headers.get("Authorization", "")
    caller_token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not caller_token or verify_token(caller_token) is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Determine which token to revoke
    token_to_revoke = req.token if req.token else caller_token
    is_current_token = token_to_revoke == caller_token

    success = revoke_token(token_to_revoke)
    if not success:
        raise HTTPException(
            status_code=500,
            detail="Failed to revoke token",
        )

    message = (
        "Current token has been revoked. Please login again."
        if is_current_token
        else "Specified token has been revoked."
    )

    return {
        "message": message,
        "revoked": True,
        "revoked_current_token": is_current_token,
    }


@router.post("/revoke-all-tokens")
async def revoke_all_sessions(request: Request):
    """Revoke all existing tokens by rotating the JWT secret.

    This endpoint requires authentication. After calling this endpoint,
    all previously issued tokens will be invalidated, and you will need
    to login again to get a new token.

    This is more efficient than revoking tokens individually when you
    want to invalidate all sessions (e.g., password reset, security incident).
    """
    if not is_auth_enabled():
        raise HTTPException(
            status_code=403,
            detail="Authentication is not enabled",
        )

    # Verify caller is authenticated
    auth_header = request.headers.get("Authorization", "")
    caller_token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not caller_token or verify_token(caller_token) is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    success = revoke_all_tokens()
    if not success:
        raise HTTPException(
            status_code=500,
            detail="Failed to revoke tokens",
        )

    return {
        "message": "All tokens have been revoked. Please login again.",
        "revoked": True,
    }
