# -*- coding: utf-8 -*-
"""CoPaw Agent - Main agent implementation.

This module provides the main CoPawAgent class built on ReActAgent,
with integrated tools, skills, and memory management.
"""

import asyncio
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, List, Literal, Optional, Type, TYPE_CHECKING

from agentscope.agent import ReActAgent
from agentscope.mcp import HttpStatefulClient, StdIOStatefulClient
from agentscope.memory import InMemoryMemory
from agentscope.message import Msg
from agentscope.tool import Toolkit
from anyio import ClosedResourceError
from pydantic import BaseModel

from .command_handler import CommandHandler
from .hooks import BootstrapHook, MemoryCompactionHook
from .model_factory import create_model_and_formatter
from .prompt import (
    build_multimodal_hint,
    build_system_prompt_from_working_dir,
    get_active_model_supports_multimodal,
)
from .skills_manager import (
    ensure_skills_initialized,
    get_working_skills_dir,
    list_available_skills,
)
from .tool_guard_mixin import ToolGuardMixin
from .tools import (
    browser_use,
    desktop_screenshot,
    edit_file,
    execute_shell_command,
    get_current_time,
    get_token_usage,
    glob_search,
    grep_search,
    read_file,
    send_file_to_user,
    set_user_timezone,
    view_image,
    write_file,
    create_memory_search_tool,
)
from .utils import process_file_and_media_blocks_in_message
from ..constant import (
    WORKING_DIR,
)
from ..agents.memory import MemoryManager

if TYPE_CHECKING:
    from ..config.config import AgentProfileConfig

logger = logging.getLogger(__name__)

# Valid namesake strategies for tool registration
NamesakeStrategy = Literal["override", "skip", "raise", "rename"]


class CoPawAgent(ToolGuardMixin, ReActAgent):
    """CoPaw Agent with integrated tools, skills, and memory management.

    This agent extends ReActAgent with:
    - Built-in tools (shell, file operations, browser, etc.)
    - Dynamic skill loading from working directory
    - Memory management with auto-compaction
    - Bootstrap guidance for first-time setup
    - System command handling (/compact, /new, etc.)
    - Tool-guard security interception (via ToolGuardMixin)

    MRO note
    ~~~~~~~~
    ``ToolGuardMixin`` overrides ``_acting`` and ``_reasoning`` via
    Python's MRO: CoPawAgent → ToolGuardMixin → ReActAgent.  If you
    add a ``_acting`` or ``_reasoning`` override in this class, you
    **must** call ``super()._acting(...)`` / ``super()._reasoning(...)``
    so the guard interception remains active.
    """

    def __init__(
        self,
        agent_config: "AgentProfileConfig",
        env_context: Optional[str] = None,
        enable_memory_manager: bool = True,
        mcp_clients: Optional[List[Any]] = None,
        memory_manager: "MemoryManager | None" = None,
        request_context: Optional[dict[str, str]] = None,
        namesake_strategy: NamesakeStrategy = "skip",
        workspace_dir: Path | None = None,
    ):
        """Initialize CoPawAgent.

        Args:
            agent_config: Agent profile configuration containing all settings
                including running config (max_iters, max_input_length,
                memory_compact_threshold, etc.) and language setting.
            env_context: Optional environment context to prepend to
                system prompt
            enable_memory_manager: Whether to enable memory manager
            mcp_clients: Optional list of MCP clients for tool
                integration
            memory_manager: Optional memory manager instance
            request_context: Optional request context with session_id,
                user_id, channel, agent_id
            namesake_strategy: Strategy to handle namesake tool functions.
                Options: "override", "skip", "raise", "rename"
                (default: "skip")
            workspace_dir: Workspace directory for reading prompt files
                (if None, uses global WORKING_DIR)
        """
        self._agent_config = agent_config
        self._env_context = env_context
        self._request_context = dict(request_context or {})
        self._mcp_clients = mcp_clients or []
        self._namesake_strategy = namesake_strategy
        self._workspace_dir = workspace_dir

        # Extract configuration from agent_config
        running_config = agent_config.running
        self._language = agent_config.language

        # Initialize toolkit with built-in tools
        toolkit = self._create_toolkit(namesake_strategy=namesake_strategy)

        # Load and register skills
        self._register_skills(toolkit)

        # Build system prompt
        sys_prompt = self._build_sys_prompt()

        # Create model and formatter using factory method
        model, formatter = create_model_and_formatter(agent_id=agent_config.id)
        model_info = (
            f"{agent_config.active_model.provider_id}/"
            f"{agent_config.active_model.model}"
            if agent_config.active_model
            else "global-fallback"
        )
        logger.info(
            f"Agent '{agent_config.id}' initialized with model: "
            f"{model_info} (class: {model.__class__.__name__})",
        )
        # Initialize parent ReActAgent
        super().__init__(
            name="Friday",
            model=model,
            sys_prompt=sys_prompt,
            toolkit=toolkit,
            memory=InMemoryMemory(),
            formatter=formatter,
            max_iters=running_config.max_iters,
        )

        # Setup memory manager
        self._setup_memory_manager(
            enable_memory_manager,
            memory_manager,
            namesake_strategy,
        )

        # Setup command handler
        self.command_handler = CommandHandler(
            agent_name=self.name,
            memory=self.memory,
            memory_manager=self.memory_manager,
            enable_memory_manager=self._enable_memory_manager,
        )

        # Register hooks
        self._register_hooks()

    def _create_toolkit(
        self,
        namesake_strategy: NamesakeStrategy = "skip",
    ) -> Toolkit:
        """Create and populate toolkit with built-in tools.

        Args:
            namesake_strategy: Strategy to handle namesake tool functions.
                Options: "override", "skip", "raise", "rename"
                (default: "skip")

        Returns:
            Configured toolkit instance
        """
        toolkit = Toolkit()

        # Check which tools are enabled from agent config
        enabled_tools = {}
        try:
            if hasattr(self._agent_config, "tools") and hasattr(
                self._agent_config.tools,
                "builtin_tools",
            ):
                builtin_tools = self._agent_config.tools.builtin_tools
                enabled_tools = {
                    name: tool.enabled for name, tool in builtin_tools.items()
                }
        except Exception as e:
            logger.warning(
                f"Failed to load agent tools config: {e}, "
                "all tools will be disabled",
            )

        # Map of tool functions
        tool_functions = {
            "execute_shell_command": execute_shell_command,
            "read_file": read_file,
            "write_file": write_file,
            "edit_file": edit_file,
            "grep_search": grep_search,
            "glob_search": glob_search,
            "browser_use": browser_use,
            "desktop_screenshot": desktop_screenshot,
            "view_image": view_image,
            "send_file_to_user": send_file_to_user,
            "get_current_time": get_current_time,
            "set_user_timezone": set_user_timezone,
            "get_token_usage": get_token_usage,
        }

        multimodal = get_active_model_supports_multimodal()

        # Register only enabled tools
        for tool_name, tool_func in tool_functions.items():
            # If tool not in config, enable by default (backward compatibility)
            if not enabled_tools.get(tool_name, True):
                logger.debug("Skipped disabled tool: %s", tool_name)
                continue

            if tool_name == "view_image" and not multimodal:
                logger.debug(
                    "Skipped view_image — model does not support multimodal",
                )
                continue

            toolkit.register_tool_function(
                tool_func,
                namesake_strategy=namesake_strategy,
            )
            logger.debug("Registered tool: %s", tool_name)

        return toolkit

    def _register_skills(self, toolkit: Toolkit) -> None:
        """Load and register skills from workspace directory.

        Args:
            toolkit: Toolkit to register skills to
        """
        workspace_dir = self._workspace_dir or WORKING_DIR

        # Check skills initialization
        ensure_skills_initialized(workspace_dir)

        working_skills_dir = get_working_skills_dir(workspace_dir)
        available_skills = list_available_skills(workspace_dir)

        for skill_name in available_skills:
            skill_dir = working_skills_dir / skill_name
            if skill_dir.exists():
                try:
                    toolkit.register_agent_skill(str(skill_dir))
                    logger.debug("Registered skill: %s", skill_name)
                except Exception as e:
                    logger.error(
                        "Failed to register skill '%s': %s",
                        skill_name,
                        e,
                    )

    def _build_sys_prompt(self) -> str:
        """Build system prompt from working dir files and env context.

        Returns:
            Complete system prompt string
        """
        # Get agent_id from request_context
        agent_id = (
            self._request_context.get("agent_id")
            if self._request_context
            else None
        )

        # Check if heartbeat is enabled in agent config
        heartbeat_enabled = False
        if (
            hasattr(self._agent_config, "heartbeat")
            and self._agent_config.heartbeat is not None
        ):
            heartbeat_enabled = self._agent_config.heartbeat.enabled

        sys_prompt = build_system_prompt_from_working_dir(
            working_dir=self._workspace_dir,
            agent_id=agent_id,
            heartbeat_enabled=heartbeat_enabled,
        )
        logger.debug("System prompt:\n%s", sys_prompt)

        # Inject multimodal capability awareness
        multimodal_hint = build_multimodal_hint()
        if multimodal_hint:
            sys_prompt = sys_prompt + "\n\n" + multimodal_hint

        if self._env_context is not None:
            sys_prompt = sys_prompt + "\n\n" + self._env_context

        return sys_prompt

    def _setup_memory_manager(
        self,
        enable_memory_manager: bool,
        memory_manager: MemoryManager | None,
        namesake_strategy: NamesakeStrategy,
    ) -> None:
        """Setup memory manager and register memory search tool if enabled.

        Args:
            enable_memory_manager: Whether to enable memory manager
            memory_manager: Optional memory manager instance
            namesake_strategy: Strategy to handle namesake tool functions
        """
        # Check env var: if ENABLE_MEMORY_MANAGER=false, disable memory manager
        env_enable_mm = os.getenv("ENABLE_MEMORY_MANAGER", "")
        if env_enable_mm.lower() == "false":
            enable_memory_manager = False

        self._enable_memory_manager: bool = enable_memory_manager
        self.memory_manager = memory_manager

        # Register memory_search tool if enabled and available
        if self._enable_memory_manager and self.memory_manager is not None:
            # update memory manager
            self.memory = self.memory_manager.get_in_memory_memory()
            self.memory_manager.chat_model = self.model
            self.memory_manager.formatter = self.formatter

            # Register memory_search as a tool function
            self.toolkit.register_tool_function(
                create_memory_search_tool(self.memory_manager),
                namesake_strategy=namesake_strategy,
            )
            logger.debug("Registered memory_search tool")

    def _register_hooks(self) -> None:
        """Register pre-reasoning and pre-acting hooks."""
        # Bootstrap hook - checks BOOTSTRAP.md on first interaction
        # Use workspace_dir if available, else fallback to WORKING_DIR
        working_dir = (
            self._workspace_dir if self._workspace_dir else WORKING_DIR
        )
        bootstrap_hook = BootstrapHook(
            working_dir=working_dir,
            language=self._language,
        )
        self.register_instance_hook(
            hook_type="pre_reasoning",
            hook_name="bootstrap_hook",
            hook=bootstrap_hook.__call__,
        )
        logger.debug("Registered bootstrap hook")

        # Memory compaction hook - auto-compact when context is full
        if self._enable_memory_manager and self.memory_manager is not None:
            memory_compact_hook = MemoryCompactionHook(
                memory_manager=self.memory_manager,
            )
            self.register_instance_hook(
                hook_type="pre_reasoning",
                hook_name="memory_compact_hook",
                hook=memory_compact_hook.__call__,
            )
            logger.debug("Registered memory compaction hook")

    def rebuild_sys_prompt(self) -> None:
        """Rebuild and replace the system prompt.

        Useful after load_session_state to ensure the prompt reflects
        the latest AGENTS.md / SOUL.md / PROFILE.md on disk.

        Updates both self._sys_prompt and the first system-role
        message stored in self.memory.content (if one exists).
        """
        self._sys_prompt = self._build_sys_prompt()

        for msg, _marks in self.memory.content:
            if msg.role == "system":
                msg.content = self.sys_prompt
            break

    async def register_mcp_clients(
        self,
        namesake_strategy: NamesakeStrategy = "skip",
    ) -> None:
        """Register MCP clients on this agent's toolkit after construction.

        Args:
            namesake_strategy: Strategy to handle namesake tool functions.
                Options: "override", "skip", "raise", "rename"
                (default: "skip")
        """
        for i, client in enumerate(self._mcp_clients):
            client_name = getattr(client, "name", repr(client))
            try:
                await self.toolkit.register_mcp_client(
                    client,
                    namesake_strategy=namesake_strategy,
                )
            except (ClosedResourceError, asyncio.CancelledError) as error:
                if self._should_propagate_cancelled_error(error):
                    raise
                logger.warning(
                    "MCP client '%s' session interrupted while listing tools; "
                    "trying recovery",
                    client_name,
                )
                recovered_client = await self._recover_mcp_client(client)
                if recovered_client is not None:
                    self._mcp_clients[i] = recovered_client
                    try:
                        await self.toolkit.register_mcp_client(
                            recovered_client,
                            namesake_strategy=namesake_strategy,
                        )
                        continue
                    except asyncio.CancelledError as recover_error:
                        if self._should_propagate_cancelled_error(
                            recover_error,
                        ):
                            raise
                        logger.warning(
                            "MCP client '%s' registration cancelled after "
                            "recovery, skipping",
                            client_name,
                        )
                    except Exception as e:  # pylint: disable=broad-except
                        logger.warning(
                            "MCP client '%s' still unavailable after "
                            "recovery, skipping: %s",
                            client_name,
                            e,
                        )
                else:
                    logger.warning(
                        "MCP client '%s' recovery failed, skipping",
                        client_name,
                    )
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(
                    "Failed to register MCP client '%s', skipping: %s",
                    client_name,
                    e,
                    exc_info=True,
                )

    async def _recover_mcp_client(self, client: Any) -> Any | None:
        """Recover MCP client from broken session and return healthy client."""
        if await self._reconnect_mcp_client(client):
            return client

        rebuilt_client = self._rebuild_mcp_client(client)
        if rebuilt_client is None:
            return None

        if await self._reconnect_mcp_client(rebuilt_client):
            return self._reuse_shared_client_reference(
                original_client=client,
                rebuilt_client=rebuilt_client,
            )

        return None

    @staticmethod
    def _reuse_shared_client_reference(
        original_client: Any,
        rebuilt_client: Any,
    ) -> Any:
        """Keep manager-shared client reference stable after rebuild."""
        original_dict = getattr(original_client, "__dict__", None)
        rebuilt_dict = getattr(rebuilt_client, "__dict__", None)
        if isinstance(original_dict, dict) and isinstance(rebuilt_dict, dict):
            original_dict.update(rebuilt_dict)
            return original_client
        return rebuilt_client

    @staticmethod
    def _should_propagate_cancelled_error(error: BaseException) -> bool:
        """Only swallow MCP-internal cancellations, not task cancellation."""
        if not isinstance(error, asyncio.CancelledError):
            return False

        task = asyncio.current_task()
        if task is None:
            return False

        cancelling = getattr(task, "cancelling", None)
        if callable(cancelling):
            return cancelling() > 0

        # Python < 3.11: Task.cancelling() is unavailable.
        # Fall back to propagating CancelledError to avoid swallowing
        # genuine task cancellations when we cannot inspect the state.
        return True

    @staticmethod
    async def _reconnect_mcp_client(
        client: Any,
        timeout: float = 60.0,
    ) -> bool:
        """Best-effort reconnect for stateful MCP clients."""
        close_fn = getattr(client, "close", None)
        if callable(close_fn):
            try:
                await close_fn()
            except asyncio.CancelledError:  # pylint: disable=try-except-raise
                raise
            except Exception:  # pylint: disable=broad-except
                pass

        connect_fn = getattr(client, "connect", None)
        if not callable(connect_fn):
            return False

        try:
            await asyncio.wait_for(connect_fn(), timeout=timeout)
            return True
        except asyncio.CancelledError:  # pylint: disable=try-except-raise
            raise
        except asyncio.TimeoutError:
            return False
        except Exception:  # pylint: disable=broad-except
            return False

    @staticmethod
    def _rebuild_mcp_client(client: Any) -> Any | None:
        """Rebuild a fresh MCP client instance from stored config metadata."""
        rebuild_info = getattr(client, "_copaw_rebuild_info", None)
        if not isinstance(rebuild_info, dict):
            return None

        transport = rebuild_info.get("transport")
        name = rebuild_info.get("name")

        try:
            if transport == "stdio":
                rebuilt_client = StdIOStatefulClient(
                    name=name,
                    command=rebuild_info.get("command"),
                    args=rebuild_info.get("args", []),
                    env=rebuild_info.get("env", {}),
                    cwd=rebuild_info.get("cwd"),
                )
                setattr(rebuilt_client, "_copaw_rebuild_info", rebuild_info)
                return rebuilt_client

            raw_headers = rebuild_info.get("headers") or {}
            headers = (
                {k: os.path.expandvars(v) for k, v in raw_headers.items()}
                if raw_headers
                else None
            )
            rebuilt_client = HttpStatefulClient(
                name=name,
                transport=transport,
                url=rebuild_info.get("url"),
                headers=headers,
            )
            setattr(rebuilt_client, "_copaw_rebuild_info", rebuild_info)
            return rebuilt_client
        except Exception:  # pylint: disable=broad-except
            return None

    # ------------------------------------------------------------------
    # Media-block fallback: strip unsupported media blocks (image, audio,
    # video) from memory and retry when the model rejects them.
    # ------------------------------------------------------------------

    _MEDIA_BLOCK_TYPES = {"image", "audio", "video"}

    def _proactive_strip_media_blocks(self) -> int:
        """Proactively strip media blocks from memory before model call.

        Only called when the active model does not support multimodal.
        Returns the number of blocks stripped.
        """
        return self._strip_media_blocks_from_memory()

    async def _reasoning(
        self,
        tool_choice: Literal["auto", "none", "required"] | None = None,
    ) -> Msg:
        """Override reasoning with proactive media filtering.

        1. Proactive layer: if the model does not support
           multimodal, strip media blocks *before* calling.
        2. Passive layer: if the model call still fails with a
           bad-request / media error, strip remaining blocks and retry.
        3. If the model IS marked as multimodal but still errors on
           media, log a warning about possibly inaccurate capability flag.

        Calls ``super()._reasoning`` to keep the ToolGuardMixin
        interception active.
        """
        # --- Proactive filtering layer ---
        if not get_active_model_supports_multimodal():
            n = self._proactive_strip_media_blocks()
            if n > 0:
                logger.warning(
                    "Proactively stripped %d media block(s) - "
                    "model does not support multimodal.",
                    n,
                )

        # --- Passive fallback layer (existing logic) ---
        try:
            return await super()._reasoning(tool_choice=tool_choice)
        except Exception as e:
            if not self._is_bad_request_or_media_error(e):
                raise

            n_stripped = self._strip_media_blocks_from_memory()
            if n_stripped == 0:
                raise

            # If the model is marked as multimodal but still
            # errored, the capability flag may be wrong.
            if get_active_model_supports_multimodal():
                logger.warning(
                    "Model marked multimodal but "
                    "rejected media. "
                    "Capability flag may be wrong.",
                )

            logger.warning(
                "_reasoning failed (%s). "
                "Stripped %d media block(s) from memory, retrying.",
                e,
                n_stripped,
            )
            return await super()._reasoning(tool_choice=tool_choice)

    async def _summarizing(self) -> Msg:
        """Override summarizing with proactive media filtering,
        passive fallback, and tool_use block filtering.

        1. Proactive layer: if the model does not support multimodal,
           strip media blocks *before* calling the model.
        2. Passive layer: if the model call still fails with a
           bad-request / media error, strip remaining blocks and retry.
        3. If the model IS marked as multimodal but still errors on
           media, log a warning about possibly inaccurate capability flag.

        Some models (e.g. kimi-k2.5) generate tool_use blocks even when
        no tools are provided.  We set ``_in_summarizing`` so that
        ``print`` can strip tool_use blocks from streaming chunks.
        """
        # --- Proactive filtering layer ---
        if not get_active_model_supports_multimodal():
            n = self._proactive_strip_media_blocks()
            if n > 0:
                logger.warning(
                    "Proactively stripped %d media block(s) - "
                    "model does not support multimodal.",
                    n,
                )

        # --- Passive fallback layer ---
        self._in_summarizing = True
        try:
            try:
                msg = await super()._summarizing()
            except Exception as e:
                if not self._is_bad_request_or_media_error(e):
                    raise

                n_stripped = self._strip_media_blocks_from_memory()
                if n_stripped == 0:
                    raise

                if get_active_model_supports_multimodal():
                    logger.warning(
                        "Model marked multimodal but "
                        "rejected media. "
                        "Capability flag may be wrong.",
                    )

                logger.warning(
                    "_summarizing failed (%s). "
                    "Stripped %d media block(s) from memory, retrying.",
                    e,
                    n_stripped,
                )
                msg = await super()._summarizing()
        finally:
            self._in_summarizing = False

        return self._strip_tool_use_from_msg(msg)

    async def print(
        self,
        msg: Msg,
        last: bool = True,
        speech: Any = None,
    ) -> None:
        """Filter tool_use blocks during _summarizing before they hit the
        message queue, preventing the frontend from briefly rendering
        phantom tool calls that will never be executed.

        On the *final* streaming event (``last=True``), append the
        round-end notice so users see it immediately instead of only
        after a page refresh.  Intermediate events that become empty
        after filtering are silently skipped to avoid blank UI flashes.
        """

        if not getattr(self, "_in_summarizing", False):
            return await super().print(msg, last, speech=speech)

        original = msg.content
        modified = False

        if isinstance(original, list):
            filtered = [
                b
                for b in original
                if not (isinstance(b, dict) and b.get("type") == "tool_use")
            ]
            if not filtered and not last:
                return
            if len(filtered) != len(original) or last:
                msg.content = filtered
                if last:
                    msg.content.append(
                        {"type": "text", "text": self._ROUND_END_NOTICE},
                    )
                modified = True
        elif isinstance(original, str) and last:
            msg.content = original + self._ROUND_END_NOTICE
            modified = True
        if modified:
            try:
                return await super().print(msg, last, speech=speech)
            finally:
                msg.content = original
        return await super().print(msg, last, speech=speech)

    _ROUND_END_NOTICE = (
        "\n\n---\n"
        "本轮调用已达最大次数，回复已终止，请继续输入。\n"
        "Maximum iterations reached for this round. "
        "Please send a new message to continue."
    )

    @staticmethod
    def _strip_tool_use_from_msg(msg: Msg) -> Msg:
        """Remove tool_use blocks from a message and append a user notice.

        When _summarizing is called without tools, some models still
        return tool_use blocks.  Those blocks can never be executed, so
        strip them and append a bilingual notice telling the user this
        round of calls has ended.
        """
        if isinstance(msg.content, str):
            msg.content += CoPawAgent._ROUND_END_NOTICE
            return msg

        filtered = [
            block
            for block in msg.content
            if not (
                isinstance(block, dict) and block.get("type") == "tool_use"
            )
        ]

        n_removed = len(msg.content) - len(filtered)
        if n_removed:
            logger.debug(
                "Stripped %d tool_use block(s) from _summarizing response",
                n_removed,
            )

        filtered.append({"type": "text", "text": CoPawAgent._ROUND_END_NOTICE})
        msg.content = filtered
        return msg

    @staticmethod
    def _is_bad_request_or_media_error(exc: Exception) -> bool:
        """Return True for 400-class or media-related model errors.

        Targets bad-request (400) errors because unsupported media
        content typically causes request validation failures.  Keyword
        matching provides an extra safety net for providers that use
        non-standard status codes.
        """
        status = getattr(exc, "status_code", None)
        if status == 400:
            return True

        error_str = str(exc).lower()
        keywords = [
            "image",
            "audio",
            "video",
            "vision",
            "multimodal",
            "image_url",
        ]
        return any(kw in error_str for kw in keywords)

    _MEDIA_PLACEHOLDER = (
        "[Media content removed - model does not support this media type]"
    )

    def _strip_media_blocks_from_memory(self) -> int:
        """Remove media blocks (image/audio/video) from all messages.

        Also strips media blocks nested inside ToolResultBlock outputs.
        Inserts placeholder text when stripping leaves content empty to
        avoid malformed API requests.

        Returns:
            Total number of media blocks removed.
        """
        media_types = self._MEDIA_BLOCK_TYPES
        total_stripped = 0

        for msg, _marks in self.memory.content:
            if not isinstance(msg.content, list):
                continue

            new_content = []
            for block in msg.content:
                if (
                    isinstance(block, dict)
                    and block.get("type") in media_types
                ):
                    total_stripped += 1
                    continue

                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and isinstance(block.get("output"), list)
                ):
                    original_len = len(block["output"])
                    block["output"] = [
                        item
                        for item in block["output"]
                        if not (
                            isinstance(item, dict)
                            and item.get("type") in media_types
                        )
                    ]
                    stripped_count = original_len - len(block["output"])
                    total_stripped += stripped_count
                    if stripped_count > 0 and not block["output"]:
                        block["output"] = self._MEDIA_PLACEHOLDER

                new_content.append(block)

            if not new_content and total_stripped > 0:
                new_content.append(
                    {"type": "text", "text": self._MEDIA_PLACEHOLDER},
                )

            msg.content = new_content

        return total_stripped

    async def reply(
        self,
        msg: Msg | list[Msg] | None = None,
        structured_model: Type[BaseModel] | None = None,
    ) -> Msg:
        """Override reply to process file blocks and handle commands.

        Args:
            msg: Input message(s) from user
            structured_model: Optional pydantic model for structured output

        Returns:
            Response message
        """
        # Set workspace_dir in context for tool functions
        from ..config.context import set_current_workspace_dir

        set_current_workspace_dir(self._workspace_dir)

        # Process file and media blocks in messages
        if msg is not None:
            await process_file_and_media_blocks_in_message(msg)

        # Check if message is a system command
        last_msg = msg[-1] if isinstance(msg, list) else msg
        query = (
            last_msg.get_text_content() if isinstance(last_msg, Msg) else None
        )

        if self.command_handler.is_command(query):
            logger.info(f"Received command: {query}")
            msg = await self.command_handler.handle_command(query)
            await self.print(msg)
            return msg

        # Fast-path for common Excel generation requests.
        if self._looks_like_excel_generation_request(query):
            excel_msg = await self._generate_excel_file_directly(query or "")
            if excel_msg is not None:
                await self.print(excel_msg, True)
                await self.memory.add(excel_msg)
                return excel_msg

        # Normal message processing
        logger.info("CoPawAgent.reply: max_iters=%s", self.max_iters)
        response = await super().reply(msg=msg, structured_model=structured_model)

        # Post-fallback: some local models explain tool intent but never call.
        if (
            self._looks_like_excel_generation_request(query)
            and not self._recent_messages_contain_file_block()
        ):
            excel_msg = await self._generate_excel_file_directly(
                query or "",
                fallback_note=True,
            )
            if excel_msg is not None:
                await self.print(excel_msg, True)
                await self.memory.add(excel_msg)
                return excel_msg

        return response

    async def interrupt(self, msg: Msg | list[Msg] | None = None) -> None:
        """Interrupt the current reply process and wait for cleanup."""
        if self._reply_task and not self._reply_task.done():
            task = self._reply_task
            task.cancel(msg)
            try:
                await task
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
            except Exception:
                logger.warning(
                    "Exception occurred during interrupt cleanup",
                    exc_info=True,
                )

    @staticmethod
    def _looks_like_excel_generation_request(query: str | None) -> bool:
        """Detect direct requests that ask us to generate an Excel file."""
        if not query:
            return False

        normalized = query.lower()
        spreadsheet_words = (
            "excel",
            "xlsx",
            ".xlsx",
            "电子表格",
            "工作簿",
            "表格",
        )
        action_words = (
            "生成",
            "创建",
            "做一个",
            "导出",
            "输出",
            "发我",
            "下载",
            "create",
            "generate",
            "make",
            "build",
            "export",
        )
        howto_words = ("怎么", "如何", "教程", "teach me", "how to")

        has_sheet_word = any(word in normalized for word in spreadsheet_words)
        has_action_word = any(word in normalized for word in action_words)
        looks_like_howto = any(word in normalized for word in howto_words)
        asks_for_delivery = any(
            word in normalized for word in ("下载", "发我", "download", "send")
        )

        if not (has_sheet_word and has_action_word):
            return False
        if looks_like_howto and not asks_for_delivery:
            return False
        return True

    def _recent_messages_contain_file_block(self, tail: int = 8) -> bool:
        """Check recent memory for file blocks to avoid duplicate fallback."""
        recent = self.memory.content[-tail:] if self.memory.content else []
        for mem_msg, _marks in reversed(recent):
            content = getattr(mem_msg, "content", None)
            if not isinstance(content, list):
                continue
            if any(
                isinstance(block, dict) and block.get("type") == "file"
                for block in content
            ):
                return True
        return False

    @staticmethod
    def _infer_excel_columns(query: str) -> list[str]:
        """Infer column names from simple prompts like: 两列（任务,负责人）."""
        pattern = re.search(r"列[（(]([^()（）]+)[)）]", query)
        if pattern:
            raw = pattern.group(1)
            cols = [c.strip() for c in re.split(r"[,，、/|]", raw) if c.strip()]
            if cols:
                return cols
        return ["任务", "负责人"]

    @staticmethod
    def _infer_excel_row_count(query: str, default_rows: int = 3) -> int:
        """Infer requested row count (e.g. '三行' or '3 rows')."""
        num_match = re.search(r"(\d+)\s*(?:行|rows?)", query, re.IGNORECASE)
        if num_match:
            return max(1, min(int(num_match.group(1)), 2000))

        cn_map = {
            "一": 1,
            "二": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
        }
        cn_match = re.search(r"([一二三四五六七八九十])\s*行", query)
        if cn_match:
            return cn_map.get(cn_match.group(1), default_rows)
        return default_rows

    @staticmethod
    def _sample_value(column: str, index: int) -> str:
        column_lower = column.lower()
        if "负责" in column or "owner" in column_lower:
            return f"成员{index}"
        if "时间" in column or "date" in column_lower:
            return datetime.now().strftime("%Y-%m-%d")
        if "状态" in column or "status" in column_lower:
            return "待处理"
        return f"{column}{index}"

    @staticmethod
    def _normalize_tool_response_blocks(tool_response: Any) -> list[dict]:
        """Convert ToolResponse blocks to plain dict blocks for Msg."""
        raw_blocks = getattr(tool_response, "content", None) or []
        normalized: list[dict] = []
        for block in raw_blocks:
            if isinstance(block, dict):
                normalized.append(block)
                continue
            model_dump = getattr(block, "model_dump", None)
            if callable(model_dump):
                normalized.append(model_dump(exclude_none=True))
                continue
            to_dict = getattr(block, "dict", None)
            if callable(to_dict):
                normalized.append(to_dict(exclude_none=True))
                continue
            normalized.append({"type": "text", "text": str(block)})
        return normalized

    async def _generate_excel_file_directly(
        self,
        query: str,
        fallback_note: bool = False,
    ) -> Msg | None:
        """Create a simple .xlsx and return it as downloadable file blocks."""
        try:
            from openpyxl import Workbook
        except Exception:
            logger.exception("Excel fallback unavailable: openpyxl import failed")
            return None

        workspace_dir = Path(self._workspace_dir or WORKING_DIR)
        output_dir = workspace_dir / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_path = output_dir / f"generated_{ts}.xlsx"

        columns = self._infer_excel_columns(query)
        row_count = self._infer_excel_row_count(query)

        wb = Workbook()
        ws = wb.active
        ws.title = "Sheet1"
        ws.append(columns)
        for i in range(1, row_count + 1):
            ws.append([self._sample_value(col, i) for col in columns])
        wb.save(file_path)

        tool_response = await send_file_to_user(str(file_path))
        normalized_blocks = self._normalize_tool_response_blocks(tool_response)
        file_blocks = [
            block
            for block in normalized_blocks
            if isinstance(block, dict) and block.get("type") == "file"
        ]
        text_blocks = [
            block
            for block in normalized_blocks
            if isinstance(block, dict)
            and block.get("type") == "text"
            and block.get("text") != "File sent successfully."
        ]
        blocks = (text_blocks + file_blocks) or normalized_blocks
        if fallback_note:
            blocks.insert(
                0,
                {
                    "type": "text",
                    "text": (
                        "模型未成功触发工具调用，已自动完成 Excel 生成并提供下载。"
                    ),
                },
            )
        else:
            blocks.insert(0, {"type": "text", "text": "Excel 已生成，点击即可下载。"})

        return Msg(self.name, blocks, "assistant")
