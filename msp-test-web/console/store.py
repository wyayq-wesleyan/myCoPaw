"""服务端会话状态。

Flask 默认把 session 放在签名 Cookie 里，菜单/案件数据很容易超过 4KB。
这里只在 Cookie 里放随机 sid，真正的数据放内存。因此请单进程运行。
"""

import threading
import time
import uuid

from flask import session

from .config import (
    DEFAULT_BASE_URL,
    DEFAULT_ELC_SYSTEM_CODE,
    DEFAULT_SSO_LOGOUT_URL,
    DEFAULT_SYSTEM_CODE,
    DEFAULT_TCMS_SYSTEM_CODE,
    STATE_TTL_SECONDS,
)

_LOCK = threading.RLock()
_STATE = {}
_LAST_PRUNE = 0

LOGIN_KEYS = (
    "user",
    "roles",
    "menus",
    "rights",
    "session_payload",
    "tssotoken",
    "msp_session_id",
    "login_at",
    "login_source",
    "login_name",
    "last_login_url",
    "last_login_response",
    "last_callback",
    "cases",
    "last_case",
)

DEFAULT_STATE = {
    "base_url": DEFAULT_BASE_URL,
    "system_code": DEFAULT_SYSTEM_CODE,
    "tcms_system_code": DEFAULT_TCMS_SYSTEM_CODE,
    "elc_system_code": DEFAULT_ELC_SYSTEM_CODE,
    "sso_logout_url": DEFAULT_SSO_LOGOUT_URL,
}


def _prune(now):
    global _LAST_PRUNE
    if now - _LAST_PRUNE < 300:
        return
    _LAST_PRUNE = now
    for sid in [
        key for key, value in _STATE.items()
        if now - value.get("_ts", 0) > STATE_TTL_SECONDS
    ]:
        _STATE.pop(sid, None)


def get_state():
    sid = session.get("sid")
    if not sid:
        sid = uuid.uuid4().hex
        session["sid"] = sid
    now = time.time()
    with _LOCK:
        _prune(now)
        state = _STATE.get(sid)
        if state is None or now - state.get("_ts", 0) > STATE_TTL_SECONDS:
            state = dict(DEFAULT_STATE)
            state["_ts"] = now
            _STATE[sid] = state
        else:
            state["_ts"] = now
        return state


def clear_login(state):
    for key in LOGIN_KEYS:
        state.pop(key, None)


def public_state(state):
    result = {key: value for key, value in state.items() if not key.startswith("_")}
    if result.get("tssotoken"):
        result["tssotoken"] = mask_token(result["tssotoken"])
    return result


def mask_token(value):
    if not value:
        return ""
    text = str(value)
    if len(text) <= 8:
        return "*" * len(text)
    return text[:4] + "..." + text[-4:]
