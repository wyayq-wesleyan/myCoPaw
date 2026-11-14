"""业务服务：聚合 middleware 的认证 / MSP / TCMS / ELC 调用。"""

from urllib.parse import quote

from .config import DEFAULT_SYSTEM_CODE
from .http import as_dict, as_list, data, ok, request, segments
from .store import get_state


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------

def login_url(state, target_url):
    return request(state, "POST", "/api/v1/sso/login-url",
                   json_body={"targetUrl": target_url})


def login_with_token(state, token, sid, sys_code):
    body = {
        "tssotoken": token,
        "sessionId": sid or "",
        "systemCode": sys_code,
    }
    response = request(state, "POST", "/api/v1/platform/session", json_body=body)
    if ok(response):
        apply_session(state, as_dict(data(response, {})), "sso-token",
                      token=token, msp_session_id=body["sessionId"])
    return response


def validate_token(state, token, sid):
    return request(state, "POST", "/api/v1/sso/validate",
                   json_body={"tssotoken": token, "sessionId": sid or ""})


def fetch_user_bundle(state, login_name, sys_code):
    user_response = request(
        state, "GET", "/api/v1/msp/users/by-login/%s" % quote(login_name, safe="")
    )
    if not ok(user_response):
        user_response = request(
            state, "GET", "/api/v1/sso/user/%s" % quote(login_name, safe="")
        )
    rights_response = request(
        state, "GET", "/api/v1/msp/user/%s/rights" % quote(login_name, safe="")
    )
    menus_response = request(
        state, "GET", "/api/v1/msp/user/%s/menus" % quote(login_name, safe=""),
        params={"systemCode": sys_code},
    )
    user = as_dict(data(user_response, {}))
    bundle = {
        "userId": user.get("userId"),
        "loginName": user.get("loginName") or login_name,
        "realName": user.get("realName"),
        "orgId": user.get("orgId"),
        "orgCode": user.get("orgCode"),
        "orgName": user.get("orgName"),
        "roles": user.get("roles") or [],
        "menus": as_list(data(menus_response, [])),
        "rights": as_list(data(rights_response, [])),
        "systemCode": sys_code,
    }
    return {
        "data": bundle,
        "user_response": user_response,
        "rights_response": rights_response,
        "menus_response": menus_response,
        "responses": [user_response, rights_response, menus_response],
    }


def login_by_username(state, login_name, sys_code):
    bundle = fetch_user_bundle(state, login_name, sys_code)
    apply_session(state, bundle["data"], "loginName（未校验 tssotoken）")
    return bundle


def apply_session(state, payload, source, token=None, msp_session_id=None):
    payload = as_dict(payload)
    state["user"] = {
        "userId": payload.get("userId"),
        "loginName": payload.get("loginName"),
        "realName": payload.get("realName"),
        "orgId": payload.get("orgId"),
        "orgCode": payload.get("orgCode"),
        "orgName": payload.get("orgName"),
    }
    state["roles"] = as_list(payload.get("roles"))
    state["menus"] = as_list(payload.get("menus"))
    state["rights"] = as_list(payload.get("rights"))
    state["session_payload"] = payload
    state["login_name"] = payload.get("loginName") or state.get("login_name")
    state["system_code"] = payload.get("systemCode") or state.get("system_code")
    state["login_at"] = time_text()
    state["login_source"] = source
    if token:
        state["tssotoken"] = token
    if msp_session_id:
        state["msp_session_id"] = msp_session_id


def time_text():
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")


def current_login_name(state):
    return state.get("login_name") or as_dict(state.get("user")).get("loginName")


def current_user_id(state):
    return as_dict(state.get("user")).get("userId")


# ---------------------------------------------------------------------------
# MSP
# ---------------------------------------------------------------------------

def msp_user(state, login_name):
    response = request(
        state, "GET", "/api/v1/sso/user/%s" % quote(login_name, safe="")
    )
    if not ok(response):
        response = request(
            state, "GET", "/api/v1/msp/users/by-login/%s" % quote(login_name, safe="")
        )
    return response


def msp_rights(state, login_name):
    return request(
        state, "GET", "/api/v1/msp/user/%s/rights" % quote(login_name, safe="")
    )


def msp_right_check(state, login_name, right_code):
    return request(
        state,
        "GET",
        "/api/v1/msp/user/%s/rights/%s"
        % (quote(login_name, safe=""), quote(right_code, safe="")),
    )


def msp_menus(state, login_name, sys_code):
    return request(
        state, "GET", "/api/v1/msp/user/%s/menus" % quote(login_name, safe=""),
        params={"systemCode": sys_code},
    )


def msp_roles(state, login_name):
    return request(
        state, "GET", "/api/v1/sso/roles/%s" % quote(login_name, safe="")
    )


def msp_org(state, login_name):
    return request(
        state, "GET", "/api/v1/sso/org/%s" % quote(login_name, safe="")
    )


def msp_systems(state):
    return request(state, "GET", "/api/v1/msp/systems")


def msp_orgs(state):
    return request(state, "GET", "/api/v1/msp/orgs")


# ---------------------------------------------------------------------------
# TCMS 案件
# ---------------------------------------------------------------------------

def tcms_user_cases(state, user_id, case_kind=None, case_status=None):
    params = {}
    if case_kind:
        params["caseKind"] = case_kind
    if case_status:
        params["caseStatus"] = case_status
    return request(
        state, "GET", "/api/v1/tcms/user/%s/cases" % quote(user_id, safe=""),
        params=params or None,
    )


def tcms_case_by_code(state, case_code):
    return request(
        state, "GET", "/api/v1/tcms/case/%s" % quote(case_code, safe="")
    )


def tcms_case_by_id(state, case_id):
    return request(
        state, "GET", "/api/v1/tcms/cases/id/%s" % quote(case_id, safe="")
    )


def tcms_archives(state, case_id, sys_code=None, kind_code=None):
    params = {}
    if sys_code:
        params["systemCode"] = sys_code
    if kind_code:
        params["archiveKindCode"] = kind_code
    return request(
        state, "GET", "/api/v1/tcms/cases/%s/archives" % quote(case_id, safe=""),
        params=params or None,
    )


def tcms_approved_archives(state, case_id, sys_code=None, kind_code=None):
    params = {}
    if sys_code:
        params["systemCode"] = sys_code
    if kind_code:
        params["archiveKindCode"] = kind_code
    return request(
        state,
        "GET",
        "/api/v1/tcms/cases/%s/archives/approved" % quote(case_id, safe=""),
        params=params or None,
    )


def tcms_archive_by_code(state, case_id, archive_code, sys_code=None, kind_code=None):
    params = {}
    if sys_code:
        params["systemCode"] = sys_code
    if kind_code:
        params["archiveKindCode"] = kind_code
    return request(
        state,
        "GET",
        "/api/v1/tcms/cases/%s/archives/%s"
        % (quote(case_id, safe=""), quote(archive_code, safe="")),
        params=params or None,
    )


def tcms_targets(state, case_id, user_id):
    return request(
        state, "GET", "/api/v1/tcms/cases/%s/targets" % quote(case_id, safe=""),
        params={"userId": user_id},
    )


def tcms_org_cases(state, user_id, org_id, status=None):
    params = {"userId": user_id, "orgId": org_id}
    if status:
        params["status"] = status
    return request(
        state, "GET", "/api/v1/tcms/cases/%s/org" % quote(org_id, safe=""),
        params=params,
    )


def tcms_flow_status(state, task_id):
    return request(
        state, "GET", "/api/v1/tcms/flow/%s/status" % quote(task_id, safe="")
    )


def tcms_flow_archive(state, flow_id):
    return request(
        state, "GET", "/api/v1/tcms/flow/%s/archive" % quote(flow_id, safe="")
    )


def tcms_start_flow(state, payload):
    return request(state, "POST", "/api/v1/tcms/flow/start", json_body=payload)


def tcms_start_common(state, payload):
    return request(state, "POST", "/api/v1/tcms/flow/start-common", json_body=payload)


def tcms_update_status(state, archive_code, status):
    return request(
        state, "PUT",
        "/api/v1/tcms/flow/%s/status" % quote(archive_code, safe=""),
        json_body={"status": status},
    )


# ---------------------------------------------------------------------------
# ELC
# ---------------------------------------------------------------------------

def elc_log(state, payload):
    return request(state, "POST", "/api/v1/elc/log", json_body=payload)


def elc_login_log(state, login_name, client_ip=None):
    params = {"loginName": login_name}
    if client_ip:
        params["clientIp"] = client_ip
    return request(state, "POST", "/api/v1/elc/log/login", params=params)


def elc_operation_log(state, params):
    return request(state, "POST", "/api/v1/elc/log/operation", params=params)


def elc_batch(state, payload_list):
    return request(state, "POST", "/api/v1/elc/logs/batch", json_body=payload_list)


def raw(state, method, path, params=None, json_body=None):
    return request(state, method, path, params=params, json_body=json_body)


def default_system(state):
    return state.get("system_code") or DEFAULT_SYSTEM_CODE
