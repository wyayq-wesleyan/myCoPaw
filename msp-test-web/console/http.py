"""middleware HTTP 客户端与响应解析。"""

import json
import re
import time
from urllib.parse import quote

import requests

from .config import DEFAULT_TIMEOUT, DEFAULT_VERIFY_TLS
from .store import get_state, mask_token


def mask_text(text):
    if text is None:
        return ""
    return re.sub(
        r"(?i)(tssotoken|token|password|secret)=([^&\s\"']+)",
        lambda m: m.group(1) + "=***",
        str(text),
    )


def first_value(mapping, keys):
    for key in keys:
        value = mapping.get(key)
        if value:
            return value
    return None


def base_url(state=None):
    state = state or get_state()
    return (state.get("base_url") or "").rstrip("/")


def system_code(state=None):
    state = state or get_state()
    return (state.get("system_code") or "").strip()


def request(state, method, path, params=None, json_body=None, timeout=None):
    method = (method or "GET").upper()
    path = (path or "").strip()
    if path.startswith("http://") or path.startswith("https://"):
        url = path
    else:
        if not path.startswith("/"):
            path = "/" + path
        url = base_url(state) + path

    started = time.time()
    try:
        response = requests.request(
            method,
            url,
            params=params,
            json=json_body,
            timeout=timeout or DEFAULT_TIMEOUT,
            verify=DEFAULT_VERIFY_TLS,
            headers={"Accept": "application/json"},
        )
        elapsed = int((time.time() - started) * 1000)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return {
            "ok": True,
            "status": response.status_code,
            "url": mask_text(response.url),
            "elapsed_ms": elapsed,
            "payload": payload,
            "text": mask_text(response.text),
            "headers": dict(response.headers),
            "error": None,
        }
    except requests.RequestException as exc:
        return {
            "ok": False,
            "status": None,
            "url": mask_text(url),
            "elapsed_ms": int((time.time() - started) * 1000),
            "payload": None,
            "text": "",
            "headers": {},
            "error": "%s: %s" % (type(exc).__name__, exc),
        }


def ok(response):
    if not response or not response.get("ok"):
        return False
    payload = response.get("payload")
    return isinstance(payload, dict) and payload.get("code") == 0


def data(response, default=None):
    if ok(response):
        value = response["payload"].get("data")
        return default if value is None else value
    return default


def message(response):
    if not response:
        return "无响应"
    if response.get("error"):
        return response["error"]
    payload = response.get("payload")
    if isinstance(payload, dict) and payload.get("message"):
        return payload["message"]
    text = response.get("text") or ""
    return text[:300] if text else "请求失败（HTTP %s）" % response.get("status")


def pretty(value):
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return mask_text(str(value))


def as_list(value):
    return value if isinstance(value, list) else []


def as_dict(value):
    return value if isinstance(value, dict) else {}


def segments(*values):
    """把路径参数逐段 URL 编码。"""
    return "/".join(quote(str(value), safe="") for value in values)
