# -*- coding: utf-8 -*-
"""Backend-enforced administrator-only tool gate.

The console hides some tools from ordinary users, but a prompt can still ask
the model to call them, and an account that can edit its own agent may enable
them again.  These tools execute host-level operations (external agents,
desktop capture, a browser with local/network reach), so the check belongs in
the tool itself, not in the UI.

Roles are intentionally only ``admin`` and ``user``.  There is no third
"advanced" role; anything that needs host-level reach is administrator-only.
"""

from __future__ import annotations

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk


def admin_denial(tool_label: str) -> str | None:
    """Return a denial message for a non-admin caller, or ``None`` for admin."""
    from .user_files import AccountScopeError, resolve_account_username
    from ...app.user_space import is_admin_user

    try:
        username = resolve_account_username()
    except AccountScopeError as exc:
        return f"无法确定当前账号，出于安全考虑已拒绝该操作：{exc}"
    try:
        if is_admin_user(username):
            return None
    except Exception:  # noqa: BLE001 - identity check failure must deny
        return "无法校验当前账号的管理员身份，出于安全考虑已拒绝该操作。"
    return (
        f"该工具仅管理员可用（{tool_label}）。"
        "普通用户不能执行宿主机级操作；如需该能力，请联系管理员。"
    )


def denial_chunk(message: str) -> ToolChunk:
    return ToolChunk(
        is_last=True,
        state=ToolResultState.DENIED,
        content=[TextBlock(type="text", text=message)],
    )
