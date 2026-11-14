"""原始 API 调试台。"""

import json

from flask import Blueprint, render_template, request

from ..config import ALLOWED_RAW_METHODS
from ..http import request as http_request
from ..services import current_login_name
from ..store import get_state

bp = Blueprint("tools", __name__, url_prefix="/tools")

PRESETS = [
    ("系统列表", "GET", "/api/v1/msp/systems", ""),
    ("组织列表", "GET", "/api/v1/msp/orgs", ""),
    ("用户权限", "GET", "/api/v1/msp/user/{loginName}/rights", ""),
    ("用户菜单", "GET", "/api/v1/msp/user/{loginName}/menus", "systemCode={systemCode}"),
    ("用户案件", "GET", "/api/v1/tcms/user/{userId}/cases", "caseStatus=1"),
    ("案件详情", "GET", "/api/v1/tcms/case/{caseCode}", ""),
    ("案件档案", "GET", "/api/v1/tcms/cases/{caseId}/archives", "systemCode={systemCode}"),
    ("案件目标", "GET", "/api/v1/tcms/cases/{caseId}/targets", "userId={userId}"),
    ("流程状态", "GET", "/api/v1/tcms/flow/{taskId}/status", ""),
    ("ELC 登录日志", "POST", "/api/v1/elc/log/login", "loginName={loginName}"),
]


@bp.route("/raw", methods=["GET", "POST"])
def raw_api():
    state = get_state()
    method = (request.values.get("method") or "GET").upper()
    path = (request.values.get("path") or "/api/v1/msp/systems").strip()
    query = (request.values.get("query") or "").strip()
    body = request.values.get("body") or ""
    response = None
    error = None

    if request.method == "POST":
        if method not in ALLOWED_RAW_METHODS:
            error = "不支持的 HTTP 方法：%s" % method
        else:
            from urllib.parse import parse_qsl
            params = parse_qsl(query, keep_blank_values=True) if query else None
            json_body = None
            if body.strip():
                try:
                    json_body = json.loads(body)
                except json.JSONDecodeError as exc:
                    error = "请求体不是合法 JSON：%s" % exc
            if not error:
                response = http_request(state, method, path, params=params, json_body=json_body)
                if response and not response.get("ok"):
                    error = response.get("error")

    presets = []
    user = state.get("user") or {}
    replacements = {
        "{loginName}": current_login_name(state) or "zhangsan",
        "{userId}": (user.get("userId") or "U1001"),
        "{caseId}": state.get("last_case_id") or "{caseId}",
        "{caseCode}": (state.get("last_case") or {}).get("caseCode") or "{caseCode}",
        "{systemCode}": state.get("system_code") or "middleware",
        "{taskId}": "{taskId}",
    }
    for name, preset_method, preset_path, preset_query in PRESETS:
        resolved_path = preset_path
        resolved_query = preset_query
        for key, value in replacements.items():
            resolved_path = resolved_path.replace(key, str(value))
            resolved_query = resolved_query.replace(key, str(value))
        presets.append((name, preset_method, resolved_path, resolved_query))

    return render_template(
        "tools/raw.html", method=method, path=path, query=query, body=body,
        response=response, error=error, presets=presets,
        login_name=current_login_name(state),
    )
