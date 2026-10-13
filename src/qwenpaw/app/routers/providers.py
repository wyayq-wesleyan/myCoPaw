# -*- coding: utf-8 -*-
"""API routes for LLM providers and models."""

from __future__ import annotations

import logging
from typing import Dict, List, Literal, Optional
from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
)
from pydantic import BaseModel, Field

from qwenpaw.exceptions import (
    AppBaseException,
)

from ..agent_context import get_agent_for_request
from ..auth import (
    get_user_info,
    has_registered_users,
    is_auth_enabled,
    require_admin_access,
)
from ..user_space import (
    first_allowed_model,
    get_request_username,
    is_model_allowed,
)
from ..utils import schedule_agent_reload
from ...config.config import load_agent_config, save_agent_config
from ...providers.provider import ProviderInfo, ModelInfo
from ...providers.model_type import infer_model_type
from ...config.config import ActiveModelsInfo
from ...providers.provider_manager import ProviderManager
from ...providers.openrouter_provider import OpenRouterProvider
from ...config.config import ModelSlotConfig

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models", tags=["models"])

ChatModelName = Literal[
    "OpenAIChatModel",
    "OpenAIResponseModel",
    "AnthropicChatModel",
    "GeminiChatModel",
    "DashScopeChatModel",
]

# effective: agent-specific if set, otherwise global
# global: the global model only, ignoring any agent-specific setting
# agent: a specific agent's model only, error if not set
ActiveModelReadScope = Literal["effective", "global", "agent"]
ActiveModelWriteScope = Literal["global", "agent"]


async def get_provider_manager(request: Request) -> ProviderManager:
    """Get the provider manager from app state.

    Args:
        request: FastAPI request object
    """
    return request.app.state.provider_manager


def _require_admin_model_access(request: Request) -> None:
    """Restrict provider configuration to the offline deployment admin."""
    if is_auth_enabled():
        require_admin_access(get_request_username(request))


def _visible_provider_info(
    provider: ProviderInfo,
    username: str | None,
) -> ProviderInfo | None:
    """Return a provider view containing only models authorized to a user."""
    if not username:
        return provider
    user = get_user_info(username)
    is_admin = bool(user and user.get("role") == "admin")
    models = [
        model
        for model in provider.models
        if is_model_allowed(username, provider.id, model.id)
    ]
    extra_models = [
        model
        for model in provider.extra_models
        if is_model_allowed(username, provider.id, model.id)
    ]
    if not models and not extra_models:
        # An administrator must be able to see a newly-created provider
        # before its first model is added. Regular users only see providers
        # for which they have at least one authorized model.
        if not is_admin:
            return None
    if is_admin:
        return provider
    # Users need model metadata for Chat, but never provider credentials.
    return provider.model_copy(
        update={"models": models, "extra_models": extra_models, "api_key": "", "custom_headers": {}},
    )


def _active_models_info(
    manager: ProviderManager,
    active_llm: ModelSlotConfig | None,
    username: str | None = None,
    allow_implicit_fallback: bool = True,
) -> ActiveModelsInfo:
    """Normalise a model slot and report the context window it implies.

    An unauthorized slot is never reported — otherwise an administrator-only
    model would leak into a regular user's chat selector after that user's
    entitlement changed. With ``allow_implicit_fallback`` the empty result is
    then filled with the account's first authorized model, which is how a
    conversation starts when nobody configured anything.
    """
    effective_max_input_length = None
    active_llm = _slot_if_authorized(active_llm, username)
    if active_llm is None and allow_implicit_fallback:
        active_llm = _implicit_default_model(username)
    if active_llm and active_llm.provider_id and active_llm.model:
        provider = manager.get_provider(active_llm.provider_id)
        if provider is not None:
            effective_max_input_length = provider.get_context_size(
                active_llm.model,
            )
    return ActiveModelsInfo(
        active_llm=active_llm,
        effective_max_input_length=effective_max_input_length,
    )


def _implicit_default_model(username: str | None) -> ModelSlotConfig | None:
    """Return the model a conversation starts on when nothing is configured.

    Administrators no longer pin a default model, so an account that has not
    chosen one yet runs on the first model it is authorized to use. Returning
    ``None`` means the account has no usable model at all, which the chat page
    surfaces as "ask your administrator".
    """
    resolved = first_allowed_model(username)
    if not resolved:
        return None
    provider_id, model_id = resolved
    return ModelSlotConfig(provider_id=provider_id, model=model_id)


def _slot_if_authorized(
    slot: ModelSlotConfig | None,
    username: str | None,
) -> ModelSlotConfig | None:
    """Keep a configured slot only when this account may actually use it.

    The legacy global slot was chosen by an administrator and may point at a
    model a regular account cannot access. Treating that as "nothing
    configured" (rather than failing) is what lets the account still start a
    conversation on its own first authorized model.
    """
    if not slot or not slot.provider_id or not slot.model:
        return None
    if username and not is_model_allowed(username, slot.provider_id, slot.model):
        return None
    return slot


class ProviderConfigRequest(BaseModel):
    api_key: Optional[str] = Field(default=None)
    base_url: Optional[str] = Field(default=None)
    name: Optional[str] = Field(
        default=None,
        description=("New display name. Only applied to custom providers."),
    )
    chat_model: Optional[ChatModelName] = Field(
        default=None,
        description="Chat model class name for protocol selection",
    )
    generate_kwargs: Optional[dict] = Field(
        default_factory=dict,
        description=(
            "Configuration in json format, will be expanded "
            "and passed to generation calls "
            "(e.g., openai.chat.completions, anthropic.messages)."
        ),
    )
    custom_headers: Optional[Dict[str, str]] = Field(
        default=None,
        description="Custom HTTP headers to include in every API request.",
    )
    auth_mode: Optional[Literal["api_key", "auth_token"]] = Field(
        default=None,
        description=(
            "Authentication mode: 'api_key' or 'auth_token'. "
            "Only applies to Anthropic-compatible providers."
        ),
    )


class ModelSlotRequest(BaseModel):
    provider_id: str = Field(..., description="Provider to use")
    model: str = Field(..., description="Model identifier")
    scope: ActiveModelWriteScope = Field(
        ...,
        description="Whether to update the global model or a specific agent",
    )
    agent_id: Optional[str] = Field(
        default=None,
        description="Target agent ID when scope is 'agent'",
    )


class CreateCustomProviderRequest(BaseModel):
    id: str = Field(...)
    name: str = Field(...)
    default_base_url: str = Field(default="")
    api_key: Optional[str] = Field(default=None)
    api_key_prefix: str = Field(default="")
    chat_model: ChatModelName = Field(default="OpenAIChatModel")
    models: List[ModelInfo] = Field(default_factory=list)


class AddModelRequest(BaseModel):
    id: str = Field(...)
    name: str = Field(...)
    model_type: Optional[Literal["chat", "embedding", "reranker"]] = Field(
        default=None,
        description="Model purpose. Omitted values are inferred from the ID.",
    )
    is_free: bool = Field(
        default=False,
        description="Whether this model is free to use",
    )
    supports_multimodal: Optional[bool] = Field(
        default=None,
        description="Whether the model supports multimodal input",
    )
    supports_image: Optional[bool] = Field(
        default=None,
        description="Whether the model supports image input",
    )
    supports_video: Optional[bool] = Field(
        default=None,
        description="Whether the model supports video input",
    )
    probe_source: Optional[str] = Field(
        default=None,
        description="Source of capability metadata",
    )


class ModelConfigRequest(BaseModel):
    model_type: Optional[Literal["chat", "embedding", "reranker"]] = Field(
        default=None,
        description="Model purpose override.",
    )
    max_tokens: Optional[int] = Field(
        default=None,
        description="Maximum output tokens per response.",
    )
    max_input_length: Optional[int] = Field(
        default=None,
        description="Maximum input context window size (tokens).",
    )
    generate_kwargs: Optional[dict] = Field(
        default_factory=dict,
        description=(
            "Per-model generation parameters in JSON format. "
            "These override provider-level generate_kwargs."
        ),
    )
    relay_reasoning: Optional[bool] = Field(
        default=None,
        description="Whether to relay reasoning_content in subsequent turns.",
    )
    thinking_enabled: Optional[bool] = Field(
        default=None,
        description="Enable/disable thinking for this model.",
    )
    thinking_budget: Optional[int] = Field(
        default=None,
        description="Token budget for thinking.",
    )
    reasoning_effort: Optional[str] = Field(
        default=None,
        description="Reasoning effort level (low/medium/high).",
    )
    supports_image: Optional[bool] = Field(
        default=None,
        description="Whether this model accepts image input.",
    )
    supports_video: Optional[bool] = Field(
        default=None,
        description="Whether this model accepts video input.",
    )


def _validate_model_slot(
    manager: ProviderManager,
    provider_id: str,
    model_id: str,
) -> None:
    """Validate that the provider and model exist without mutating state."""
    provider = manager.get_provider(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=404,
            detail=f"Provider '{provider_id}' not found.",
        )
    if not provider.has_model(model_id):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model '{model_id}' not found in provider '{provider_id}'."
            ),
        )
    model = provider.get_model_info(model_id)
    if model is not None and model.model_type != "chat":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model '{model_id}' is configured as {model.model_type}; "
                "only chat models can be selected for conversations."
            ),
        )


async def _load_agent_model(
    request: Request,
    agent_id: str,
) -> ModelSlotConfig | None:
    """Load the model configured for a specific agent."""
    workspace = await get_agent_for_request(request, agent_id=agent_id)
    agent_config = load_agent_config(workspace.agent_id)
    return agent_config.active_model


@router.get(
    "",
    response_model=List[ProviderInfo],
    summary="List all providers",
)
async def list_all_providers(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
) -> List[ProviderInfo]:
    username = get_request_username(request)
    if is_auth_enabled() and has_registered_users() and not username:
        return []
    providers = await manager.list_provider_info()
    visible = [_visible_provider_info(provider, username) for provider in providers]
    user = get_user_info(username) if username else None
    if user and user.get("role") == "admin":
        # ``Provider.get_info`` masks secrets by design. The offline admin
        # needs the actual saved value to view and edit an internal endpoint;
        # regular users never reach this branch.
        visible = [
            provider.model_copy(
                update={
                    "api_key": runtime.api_key,
                    "custom_headers": runtime.custom_headers,
                },
            )
            if provider is not None
            and (runtime := manager.get_provider(provider.id)) is not None
            else provider
            for provider in visible
        ]
    return [provider for provider in visible if provider is not None]


@router.put(
    "/{provider_id}/config",
    response_model=ProviderInfo,
    summary="Configure a provider",
)
async def configure_provider(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    body: ProviderConfigRequest = Body(...),
) -> ProviderInfo:
    _require_admin_model_access(request)
    config = {
        "api_key": body.api_key,
        "base_url": body.base_url,
        "chat_model": body.chat_model,
        "generate_kwargs": body.generate_kwargs,
        "custom_headers": body.custom_headers,
        "auth_mode": body.auth_mode,
    }
    # Renaming is restricted to custom providers so built-in
    # provider names stay immutable.
    name = body.name.strip() if body.name else None
    if name:
        provider = manager.get_provider(provider_id)
        if provider is not None and provider.is_custom:
            config["name"] = name
    ok = manager.update_provider(provider_id, config)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"Provider '{provider_id}' not found",
        )

    provider_info = await manager.get_provider_info(provider_id)
    if provider_info is None:
        raise HTTPException(
            status_code=404,
            detail=f"Provider '{provider_id}' not found after update",
        )
    return provider_info


@router.post(
    "/custom-providers",
    response_model=ProviderInfo,
    summary="Create a custom provider",
    status_code=201,
)
async def create_custom_provider_endpoint(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
    body: CreateCustomProviderRequest = Body(...),
) -> ProviderInfo:
    _require_admin_model_access(request)
    try:
        provider_info = await manager.add_custom_provider(
            ProviderInfo(
                id=body.id,
                name=body.name,
                base_url=body.default_base_url,
                api_key=body.api_key or "",
                api_key_prefix=body.api_key_prefix,
                chat_model=body.chat_model,
                extra_models=body.models,
            ),
        )
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return provider_info


class TestConnectionResponse(BaseModel):
    success: bool = Field(..., description="Whether the test passed")
    message: str = Field(..., description="Human-readable result message")


class TestProviderRequest(BaseModel):
    api_key: Optional[str] = Field(
        default=None,
        description="Optional API key to test",
    )
    base_url: Optional[str] = Field(
        default=None,
        description="Optional Base URL to test",
    )
    chat_model: Optional[ChatModelName] = Field(
        default=None,
        description="Optional chat model class to test protocol behavior",
    )
    custom_headers: Optional[Dict[str, str]] = Field(
        default=None,
        description="Custom headers to use for this test request",
    )
    auth_mode: Optional[Literal["api_key", "auth_token"]] = Field(
        default=None,
        description="Authentication mode to use for this test request",
    )


class TestModelRequest(BaseModel):
    model_id: str = Field(..., description="Model ID to test")


class DiscoverModelsRequest(BaseModel):
    api_key: Optional[str] = Field(
        default=None,
        description="Optional API key to use for discovery",
    )
    base_url: Optional[str] = Field(
        default=None,
        description="Optional Base URL to use for discovery",
    )
    chat_model: Optional[ChatModelName] = Field(
        default=None,
        description="Optional chat model class to use for discovery",
    )


class DiscoverModelsResponse(BaseModel):
    success: bool = Field(..., description="Whether discovery succeeded")
    models: List[ModelInfo] = Field(
        default_factory=list,
        description="Discovered models",
    )
    message: str = Field(
        default="",
        description="Human-readable result message",
    )
    added_count: int = Field(
        default=0,
        description="How many new models were added into provider config",
    )


@router.post(
    "/{provider_id}/test",
    response_model=TestConnectionResponse,
    summary="Test provider connection",
)
async def test_provider(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    body: Optional[TestProviderRequest] = Body(default=None),
) -> TestConnectionResponse:
    """Test if a provider's URL and API key are valid."""
    _require_admin_model_access(request)
    try:
        provider = manager.get_provider(provider_id)
        if provider is None:
            raise ValueError(f"Provider '{provider_id}' not found")
        # Build a lightweight Pydantic copy with only the overridden fields;
        # avoids deepcopy which fails when _strip_http_client is cached.
        overrides: dict = {}
        if body and body.api_key:
            overrides["api_key"] = body.api_key
        if body and body.base_url:
            overrides["base_url"] = body.base_url
        if body and body.custom_headers is not None:
            overrides["custom_headers"] = body.custom_headers
        if body and body.auth_mode in ("api_key", "auth_token"):
            overrides["auth_mode"] = body.auth_mode
        tmp_provider = provider.model_copy(update=overrides)
        ok, msg = await tmp_provider.check_connection()
        return TestConnectionResponse(
            success=ok,
            message=(
                "Connection successful" if ok else f"Connection failed: {msg}"
            ),
        )
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/{provider_id}/discover",
    response_model=DiscoverModelsResponse,
    summary="Discover available models from provider",
)
async def discover_models(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    body: Optional[DiscoverModelsRequest] = Body(default=None),
    save: bool = Query(
        default=True,
        description="Save discovered models to provider",
    ),
) -> DiscoverModelsResponse:
    _require_admin_model_access(request)
    try:
        provider = manager.get_provider(provider_id)
        if provider is None:
            raise HTTPException(
                status_code=404,
                detail=f"Provider '{provider_id}' not found",
            )

        existing_model_ids = {
            model.id for model in provider.models + provider.extra_models
        }

        ok = manager.update_provider(
            provider_id,
            {
                "api_key": body.api_key if body else None,
                "base_url": body.base_url if body else None,
            },
        )
        if not ok:
            raise HTTPException(
                status_code=404,
                detail=f"Provider '{provider_id}' not found",
            )
        try:
            result = await manager.fetch_provider_models(
                provider_id,
                save=save,
            )
            success = bool(result)
        except Exception:
            result = []
            success = False

        added_count = 0
        if save and success:
            added_count = sum(
                1 for model in result if model.id not in existing_model_ids
            )

        return DiscoverModelsResponse(
            success=success,
            models=result,
            message=(
                ""
                if success
                else "No models found. Check the internal service address and API Key."
            ),
            added_count=added_count,
        )
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/{provider_id}/models/test",
    response_model=TestConnectionResponse,
    summary="Test a specific model",
)
async def test_model(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    body: TestModelRequest = Body(...),
) -> TestConnectionResponse:
    """Test if a specific model works with the configured provider."""
    _require_admin_model_access(request)
    try:
        provider = manager.get_provider(provider_id)
        if provider is None:
            raise ValueError(f"Provider '{provider_id}' not found")
        ok, msg = await provider.check_model_connection(model_id=body.model_id)
        return TestConnectionResponse(
            success=ok,
            message=(
                "Model connection successful"
                if ok
                else f"Model connection failed: {msg}"
            ),
        )
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete(
    "/custom-providers/{provider_id}",
    response_model=List[ProviderInfo],
    summary="Delete a custom provider",
)
async def delete_custom_provider_endpoint(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
) -> List[ProviderInfo]:
    _require_admin_model_access(request)
    try:
        ok = manager.remove_custom_provider(provider_id)
        if not ok:
            raise ValueError(f"Custom Provider '{provider_id}' not found")
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await manager.list_provider_info()


@router.post(
    "/{provider_id}/models",
    response_model=ProviderInfo,
    summary="Add a model to a provider",
    status_code=201,
)
async def add_model_endpoint(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    body: AddModelRequest = Body(...),
) -> ProviderInfo:
    _require_admin_model_access(request)
    try:
        provider = await manager.add_model_to_provider(
            provider_id=provider_id,
            model_info=ModelInfo(
                id=body.id,
                name=body.name,
                supports_multimodal=body.supports_multimodal,
                supports_image=body.supports_image,
                supports_video=body.supports_video,
                probe_source=body.probe_source,
                is_free=body.is_free,
                model_type=body.model_type or infer_model_type(body.id, body.name),
                model_type_source="manual" if body.model_type else "auto",
            ),
        )  # Validate provider exists and add model
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return provider


class ProbeMultimodalResponse(BaseModel):
    supports_image: bool = Field(
        default=False,
        description="Whether the model supports image input",
    )
    supports_video: bool = Field(
        default=False,
        description="Whether the model supports video input",
    )
    supports_multimodal: bool = Field(
        default=False,
        description="Whether the model supports any multimodal input",
    )
    image_message: str = Field(
        default="",
        description="Probe result message for image support",
    )
    video_message: str = Field(
        default="",
        description="Probe result message for video support",
    )


@router.post(
    "/{provider_id}/models/{model_id:path}/probe-multimodal",
    response_model=ProbeMultimodalResponse,
    summary="Probe model multimodal capability",
)
async def probe_model_multimodal(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    model_id: str = Path(...),
) -> ProbeMultimodalResponse:
    """Probe image and video support by sending lightweight test requests."""
    _require_admin_model_access(request)
    result = await manager.probe_model_multimodal(provider_id, model_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return ProbeMultimodalResponse(**result)


@router.delete(
    "/{provider_id}/models/{model_id:path}",
    response_model=ProviderInfo,
    summary="Remove a model from a provider",
)
async def remove_model_endpoint(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    model_id: str = Path(...),
) -> ProviderInfo:
    _require_admin_model_access(request)
    try:
        provider = await manager.delete_model_from_provider(
            provider_id=provider_id,
            model_id=model_id,
        )  # Validate provider and model exist and delete
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return provider


@router.put(
    "/{provider_id}/models/{model_id:path}/config",
    response_model=ProviderInfo,
    summary="Configure per-model generation parameters",
)
async def configure_model(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    model_id: str = Path(...),
    body: ModelConfigRequest = Body(...),
) -> ProviderInfo:
    """Update per-model generate_kwargs that override provider-level
    settings."""
    _require_admin_model_access(request)
    try:
        provider_info = await manager.update_model_config(
            provider_id=provider_id,
            model_id=model_id,
            config={
                "generate_kwargs": body.generate_kwargs,
                "max_tokens": body.max_tokens,
                "max_input_length": body.max_input_length,
                "relay_reasoning": body.relay_reasoning,
                "thinking_enabled": body.thinking_enabled,
                "thinking_budget": body.thinking_budget,
                "reasoning_effort": body.reasoning_effort,
                "supports_image": body.supports_image,
                "supports_video": body.supports_video,
                "supports_multimodal": (
                    body.supports_image or body.supports_video
                    if body.supports_image is not None
                    or body.supports_video is not None
                    else None
                ),
                "model_type": body.model_type,
            },
        )
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return provider_info


@router.get(
    "/active",
    response_model=ActiveModelsInfo,
    summary="Get effective active LLM",
)
async def get_active_models(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
    scope: ActiveModelReadScope = Query(default="effective"),
    agent_id: Optional[str] = Query(default=None),
) -> ActiveModelsInfo:
    """Get active model by scope.

    - effective: agent-specific first, then the account's first authorized model
    - global: the account's first authorized model
    - agent: a specific agent's configured model only
    """
    username = get_request_username(request)
    if scope == "global":
        # No administrator-pinned default exists any more, so this reports the
        # account's first authorized model. A legacy global value is still
        # honoured when it is authorized, so existing installs keep the model
        # they were already running.
        return _active_models_info(
            manager,
            manager.get_active_model(),
            username,
        )

    if scope == "agent":
        if not agent_id:
            raise HTTPException(
                status_code=400,
                detail="agent_id is required when scope is 'agent'",
            )
        return _active_models_info(
            manager,
            await _load_agent_model(request, agent_id),
            username,
        )

    try:
        target_agent_id = agent_id
        if target_agent_id is None:
            workspace = await get_agent_for_request(request)
            target_agent_id = workspace.agent_id

        agent_model = await _load_agent_model(request, target_agent_id)
        if agent_model:
            logger.info(
                "Returning agent-specific model for %s: %s",
                target_agent_id,
                agent_model,
            )
            return _active_models_info(manager, agent_model, username)
    except (
        HTTPException,
        OSError,
        ValueError,
        TypeError,
        AppBaseException,
    ) as exc:
        logger.warning(
            "Failed to get agent-specific model: %s",
            exc,
            exc_info=True,
        )

    # Nothing was explicitly chosen, so ``_active_models_info`` fills in the
    # first authorized model. That is what makes a brand-new conversation
    # usable without an administrator setting a default.
    fallback = manager.get_active_model()
    logger.info("Returning fallback model: %s", fallback)
    return _active_models_info(manager, fallback, username)


@router.put(
    "/active",
    response_model=ActiveModelsInfo,
    summary="Set active LLM",
)
async def set_active_model(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
    body: ModelSlotRequest = Body(...),
) -> ActiveModelsInfo:
    """Set active model by scope."""
    username = get_request_username(request)
    if body.scope == "global":
        _require_admin_model_access(request)
        try:
            await manager.activate_model(body.provider_id, body.model)
        except (
            FileNotFoundError,
            RuntimeError,
            ValueError,
            AppBaseException,
        ) as exc:
            message = str(exc)
            lower_msg = message.lower()
            if "provider" in lower_msg and "not found" in lower_msg:
                raise HTTPException(status_code=404, detail=message) from exc
            raise HTTPException(status_code=400, detail=message) from exc

        # Sync to active agent if its active_model is unset (#4937)
        try:
            workspace = await get_agent_for_request(request)
            agent_config = load_agent_config(workspace.agent_id)
            if (
                not agent_config.active_model
                or not agent_config.active_model.provider_id
            ):
                agent_config.active_model = ModelSlotConfig(
                    provider_id=body.provider_id,
                    model=body.model,
                )
                save_agent_config(workspace.agent_id, agent_config)
                schedule_agent_reload(request, workspace.agent_id)
        except Exception:
            pass

        return _active_models_info(
            manager,
            manager.get_active_model(),
            username,
        )

    if not body.agent_id:
        raise HTTPException(
            status_code=400,
            detail="agent_id is required when scope is 'agent'",
        )

    if not is_model_allowed(username, body.provider_id, body.model):
        raise HTTPException(status_code=403, detail="Model is not authorized for this user")

    _validate_model_slot(manager, body.provider_id, body.model)

    try:
        workspace = await get_agent_for_request(
            request,
            agent_id=body.agent_id,
        )
        agent_config = load_agent_config(workspace.agent_id)
        agent_config.active_model = ModelSlotConfig(
            provider_id=body.provider_id,
            model=body.model,
        )
        save_agent_config(workspace.agent_id, agent_config)
        # Hot reload agent (async, non-blocking)
        schedule_agent_reload(request, workspace.agent_id)

    except (
        HTTPException,
        OSError,
        ValueError,
        TypeError,
        AppBaseException,
    ) as exc:
        logger.warning(
            "Failed to save active model to agent config: %s",
            exc,
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to save active model to agent config",
        ) from exc

    manager.maybe_probe_multimodal(body.provider_id, body.model)

    return _active_models_info(
        manager,
        ModelSlotConfig(
            provider_id=body.provider_id,
            model=body.model,
        ),
        username,
    )


# =============================================================================
# OpenRouter-specific endpoints for model discovery with filtering
# =============================================================================


class FilterModelsRequest(BaseModel):
    """Request model for filtering OpenRouter models."""

    providers: List[str] = Field(
        default_factory=list,
        description="Filter by provider/series (e.g., ['openai', 'google'])",
    )
    input_modalities: List[str] = Field(
        default_factory=list,
        description="Required input modalities (e.g., ['image'])",
    )
    output_modalities: List[str] = Field(
        default_factory=list,
        description="Required output modalities (e.g., ['text'])",
    )
    max_prompt_price: Optional[float] = Field(
        default=None,
        description="Maximum prompt price per 1M tokens (e.g., 0.000001)",
    )
    is_free: Optional[bool] = Field(
        default=None,
        description="Whether to return only free models",
    )


class SeriesResponse(BaseModel):
    """Response model for available series/providers."""

    series: List[str] = Field(
        default_factory=list,
        description="Provider series (e.g., ['openai', 'google'])",
    )


class DiscoverExtendedResponse(BaseModel):
    """Response model for extended model discovery."""

    success: bool = Field(..., description="Whether discovery succeeded")
    models: List[dict] = Field(
        default_factory=list,
        description="Discovered models with extended metadata",
    )
    providers: List[str] = Field(
        default_factory=list,
        description="Available provider series",
    )
    total_count: int = Field(
        default=0,
        description="Total number of models discovered",
    )


class FilterModelsResponse(BaseModel):
    """Response model for filtered models."""

    success: bool = Field(..., description="Whether filtering succeeded")
    models: List[dict] = Field(
        default_factory=list,
        description="Filtered models with extended metadata",
    )
    total_count: int = Field(
        default=0,
        description="Total number of models matching filters",
    )


@router.get(
    "/openrouter/series",
    response_model=SeriesResponse,
    summary="Get available OpenRouter provider series",
)
async def get_openrouter_series(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
) -> SeriesResponse:
    """Get list of available provider/series from OpenRouter."""
    _require_admin_model_access(request)
    provider = manager.get_provider("openrouter")
    if provider is None:
        raise HTTPException(
            status_code=404,
            detail="OpenRouter provider not found",
        )

    if not isinstance(provider, OpenRouterProvider):
        raise HTTPException(
            status_code=400,
            detail="Provider is not an OpenRouter provider",
        )

    try:
        series = await provider.get_available_providers()
        return SeriesResponse(series=series)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch series: {str(exc)}",
        ) from exc


@router.post(
    "/openrouter/discover-extended",
    response_model=DiscoverExtendedResponse,
    summary="Discover OpenRouter models with extended metadata",
)
async def discover_openrouter_extended(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
    body: Optional[DiscoverModelsRequest] = Body(default=None),
) -> DiscoverExtendedResponse:
    """Discover available models from OpenRouter with full metadata."""
    _require_admin_model_access(request)
    provider = manager.get_provider("openrouter")
    if provider is None:
        raise HTTPException(
            status_code=404,
            detail="OpenRouter provider not found",
        )

    if not isinstance(provider, OpenRouterProvider):
        raise HTTPException(
            status_code=400,
            detail="Provider is not an OpenRouter provider",
        )

    if body and body.api_key:
        manager.update_provider("openrouter", {"api_key": body.api_key})

    try:
        models = await provider.fetch_extended_models()
        providers = await provider.get_available_providers()

        models_dict = [
            {
                "id": m.id,
                "name": m.name,
                "supports_multimodal": m.supports_multimodal,
                "supports_image": m.supports_image,
                "supports_video": m.supports_video,
                "probe_source": m.probe_source,
                "is_free": m.is_free,
                "provider": m.provider,
                "input_modalities": m.input_modalities,
                "output_modalities": m.output_modalities,
                "pricing": m.pricing,
            }
            for m in models
        ]

        return DiscoverExtendedResponse(
            success=True,
            models=models_dict,
            providers=providers,
            total_count=len(models_dict),
        )
    except Exception:
        return FilterModelsResponse(
            success=False,
            models=[],
            providers=[],
            total_count=0,
        )


class DefaultModelsResponse(BaseModel):
    """Response containing default chat, embedding and reranker model configurations."""
    chat_base_url: str = Field(default="", description="Base URL for chat model")
    chat_api_key: str = Field(default="", description="API key for chat model")
    chat_model: str = Field(default="", description="Default chat model name")
    embedding_base_url: str = Field(default="", description="Base URL for embedding model")
    embedding_api_key: str = Field(default="", description="API key for embedding model")
    embedding_model: str = Field(default="", description="Default embedding model name")
    embedding_dimensions: int | None = Field(default=None, description="Embedding dimensions")
    reranker_base_url: str = Field(default="", description="Base URL for reranker model")
    reranker_api_key: str = Field(default="", description="API key for reranker model")
    reranker_model: str = Field(default="", description="Default reranker model name")
    reranker_enabled: bool = Field(default=False, description="Whether reranker is enabled")


@router.get(
    "/default-models",
    response_model=DefaultModelsResponse,
    summary="Get default embedding and reranker model configurations",
)
async def get_default_models(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
) -> DefaultModelsResponse:
    """Get default chat, embedding and reranker model configurations from providers.

    This endpoint is used by the knowledge service to automatically configure
    models based on the administrator's provider settings.

    Administrator-only: the response carries provider credentials
    (``chat_api_key`` / ``embedding_api_key`` / ``reranker_api_key``) in
    plaintext, so it is not safe to expose to ordinary accounts. The check is
    skipped when authentication is disabled, where there is only one account.
    """
    _require_admin_model_access(request)

    chat_config = {"base_url": "", "api_key": "", "model": ""}
    embedding_config = {"base_url": "", "api_key": "", "model": "", "dimensions": None}
    reranker_config = {"base_url": "", "api_key": "", "model": "", "enabled": False}

    # Search through all providers for chat, embedding and reranker models
    for provider in manager.builtin_providers.values():
        provider_info = await provider.get_info()
        for model in provider_info.models + provider_info.extra_models:
            if model.model_type == "chat" and not chat_config["model"]:
                runtime = manager.get_provider(provider.id)
                chat_config = {
                    "base_url": provider_info.default_base_url or "",
                    "api_key": runtime.api_key if runtime else "",
                    "model": model.id,
                }
            elif model.model_type == "embedding" and not embedding_config["model"]:
                runtime = manager.get_provider(provider.id)
                embedding_config = {
                    "base_url": provider_info.default_base_url or "",
                    "api_key": runtime.api_key if runtime else "",
                    "model": model.id,
                    "dimensions": None,
                }
            elif model.model_type == "reranker" and not reranker_config["model"]:
                runtime = manager.get_provider(provider.id)
                reranker_config = {
                    "base_url": provider_info.default_base_url or "",
                    "api_key": runtime.api_key if runtime else "",
                    "model": model.id,
                    "enabled": True,
                }

    # Also check custom providers
    for provider in manager.custom_providers.values():
        provider_info = await provider.get_info()
        for model in provider_info.models + provider_info.extra_models:
            if model.model_type == "chat" and not chat_config["model"]:
                runtime = manager.get_provider(provider.id)
                chat_config = {
                    "base_url": provider_info.default_base_url or "",
                    "api_key": runtime.api_key if runtime else "",
                    "model": model.id,
                }
            elif model.model_type == "embedding" and not embedding_config["model"]:
                runtime = manager.get_provider(provider.id)
                embedding_config = {
                    "base_url": provider_info.default_base_url or "",
                    "api_key": runtime.api_key if runtime else "",
                    "model": model.id,
                    "dimensions": None,
                }
            elif model.model_type == "reranker" and not reranker_config["model"]:
                runtime = manager.get_provider(provider.id)
                reranker_config = {
                    "base_url": provider_info.default_base_url or "",
                    "api_key": runtime.api_key if runtime else "",
                    "model": model.id,
                    "enabled": True,
                }

    return DefaultModelsResponse(
        chat_base_url=chat_config["base_url"],
        chat_api_key=chat_config["api_key"],
        chat_model=chat_config["model"],
        embedding_base_url=embedding_config["base_url"],
        embedding_api_key=embedding_config["api_key"],
        embedding_model=embedding_config["model"],
        embedding_dimensions=embedding_config["dimensions"],
        reranker_base_url=reranker_config["base_url"],
        reranker_api_key=reranker_config["api_key"],
        reranker_model=reranker_config["model"],
        reranker_enabled=reranker_config["enabled"],
    )


@router.post(
    "/openrouter/models/filter",
    response_model=FilterModelsResponse,
    summary="Filter OpenRouter models by criteria",
)
async def filter_openrouter_models(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
    body: FilterModelsRequest = Body(...),
) -> FilterModelsResponse:
    """Filter OpenRouter models by provider, modalities, and price."""
    _require_admin_model_access(request)
    provider = manager.get_provider("openrouter")
    if provider is None:
        raise HTTPException(
            status_code=404,
            detail="OpenRouter provider not found",
        )

    if not isinstance(provider, OpenRouterProvider):
        raise HTTPException(
            status_code=400,
            detail="Provider is not an OpenRouter provider",
        )

    try:
        models = await provider.fetch_extended_models()

        filtered_models = provider.filter_models(
            models=models,
            providers=body.providers if body.providers else None,
            input_modalities=(
                body.input_modalities if body.input_modalities else None
            ),
            output_modalities=(
                body.output_modalities if body.output_modalities else None
            ),
            max_prompt_price=body.max_prompt_price,
            is_free=body.is_free,
        )

        models_dict = [
            {
                "id": m.id,
                "name": m.name,
                "supports_multimodal": m.supports_multimodal,
                "supports_image": m.supports_image,
                "supports_video": m.supports_video,
                "probe_source": m.probe_source,
                "is_free": m.is_free,
                "provider": m.provider,
                "input_modalities": m.input_modalities,
                "output_modalities": m.output_modalities,
                "pricing": m.pricing,
            }
            for m in filtered_models
        ]

        return FilterModelsResponse(
            success=True,
            models=models_dict,
            total_count=len(models_dict),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to filter models: {str(exc)}",
        ) from exc
