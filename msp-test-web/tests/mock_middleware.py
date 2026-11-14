#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无平台环境时使用的假 middleware，覆盖 MSP / TCMS / ELC 三类接口。"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import Flask, jsonify, redirect, request

app = Flask(__name__)

ROLES = [
    {"roleCode": "admin", "roleName": "管理员"},
    {"roleCode": "operator", "roleName": "操作员"},
]

MENUS = [
    {"menuId": "M100", "menuCode": "dashboard", "menuName": "工作台",
     "menuUrl": "/dashboard", "parentCode": None, "orderNum": 1, "icon": "home"},
    {"menuId": "M200", "menuCode": "case", "menuName": "案件管理",
     "menuUrl": "/case", "parentCode": None, "orderNum": 2, "icon": "folder"},
    {"menuId": "M210", "menuCode": "case-query", "menuName": "案件查询",
     "menuUrl": "/case/query", "parentCode": "M200", "orderNum": 1, "icon": "search"},
    {"menuId": "M220", "menuCode": "case-export", "menuName": "案件导出",
     "menuUrl": "/case/export", "parentCode": "M200", "orderNum": 2, "icon": "download"},
    {"menuId": "M300", "menuCode": "system", "menuName": "系统管理",
     "menuUrl": "/system", "parentCode": None, "orderNum": 3, "icon": "settings"},
]

RIGHTS = ["case:query", "case:export", "case:approve",
          "menu:dashboard", "menu:case", "menu:system"]

USER = {
    "userId": "U1001",
    "loginName": "zhangsan",
    "realName": "张三",
    "orgId": "ORG-001",
    "orgCode": "ORG001",
    "orgName": "测试部门",
    "roles": ROLES,
}

CASES = [
    {
        "caseId": "CASE-2026-0001",
        "caseCode": "A202600001",
        "caseName": "测试刑事案件一号",
        "caseType": "caseType.1",
        "caseTypeName": "刑事案件",
        "caseKindId": "caseKind.28",
        "status": "1",
        "createTime": "2026-01-05 09:12:00",
        "creator": "张三",
        "caseDeptOne": "ORG-001",
        "caseDeptOneName": "测试部门",
        "principal": "张三",
        "beginTime": "2026-01-05",
        "matureTime": "2026-06-05",
        "endTime": None,
    },
    {
        "caseId": "CASE-2026-0002",
        "caseCode": "A202600002",
        "caseName": "测试情报案件二号",
        "caseType": "caseType.2",
        "caseTypeName": "情报案件",
        "caseKindId": "caseKind.29",
        "status": "1",
        "createTime": "2026-02-11 15:40:00",
        "creator": "李四",
        "caseDeptOne": "ORG-001",
        "caseDeptOneName": "测试部门",
        "principal": "李四",
        "beginTime": "2026-02-11",
        "matureTime": "2026-08-11",
        "endTime": None,
    },
]

ARCHIVES = [
    {
        "archiveCode": "ARC-20260101-001",
        "archiveName": "数据查询审批表",
        "archiveStatus": "2",
        "createDate": "2026-01-06 10:20:00",
        "caseCode": "A202600001",
        "caseName": "测试刑事案件一号",
        "archiveKindName": "数据查询审批表",
        "archiveKindCode": "1217",
        "approveResult": "0",
        "fillingUserName": "张三",
    },
    {
        "archiveCode": "ARC-20260102-002",
        "archiveName": "数据分析审批表",
        "archiveStatus": "1",
        "createDate": "2026-01-07 11:00:00",
        "caseCode": "A202600001",
        "caseName": "测试刑事案件一号",
        "archiveKindName": "数据分析审批表",
        "archiveKindCode": "1218",
        "approveResult": "1",
        "fillingUserName": "张三",
    },
]

TARGETS = [
    {
        "caseId": "CASE-2026-0001",
        "caseCode": "A202600001",
        "caseName": "测试刑事案件一号",
        "targetId": "T-001",
        "targetName": "嫌疑人甲",
        "numberId": "N-001",
        "targetNum": "13800000001",
        "nickname": "甲某",
        "numberType": "手机",
        "netWorkName": "中国移动",
        "activeStatus": "1",
        "status": "1",
        "spyBeginTime": "2026-01-06",
        "spyEndTime": "2026-02-06",
    }
]

FLOWS = {
    "TASK-001": {
        "flowId": "ARC-20260101-001",
        "taskId": "TASK-001",
        "status": "0",
        "statusName": "审批完成",
        "approvalResult": "审批完成",
    }
}

SESSION = {
    "userId": USER["userId"],
    "loginName": USER["loginName"],
    "realName": USER["realName"],
    "orgId": USER["orgId"],
    "orgCode": USER["orgCode"],
    "orgName": USER["orgName"],
    "roles": ROLES,
    "menus": MENUS,
    "rights": RIGHTS,
    "systemCode": "middleware",
}

ELC_LOGS = []


def ok(data):
    return jsonify({"code": 0, "message": "success", "data": data})


def fail(msg):
    return jsonify({"code": 1, "message": msg, "data": None})


# ---------------------------------------------------------------- auth

@app.post("/api/v1/sso/login-url")
def login_url():
    body = request.get_json(silent=True) or {}
    target = body.get("targetUrl") or "http://127.0.0.1:18080/auth/callback"
    return ok("http://127.0.0.1:19089/mock-sso/login?" + urlencode({"targetUrl": target}))


@app.get("/mock-sso/login")
def mock_login():
    target = request.args.get("targetUrl") or "http://127.0.0.1:18080/auth/callback"
    parts = urlsplit(target)
    query = [item for item in parse_qsl(parts.query, keep_blank_values=True)
             if item[0] not in {"tssotoken", "username"}]
    query.extend([("tssotoken", "mock-token"), ("username", "zhangsan")])
    return redirect(urlunsplit((parts.scheme, parts.netloc, parts.path,
                                urlencode(query), parts.fragment)))


@app.post("/api/v1/platform/session")
def platform_session():
    body = request.get_json(silent=True) or {}
    if body.get("tssotoken") != "mock-token":
        return fail("mock: invalid tssotoken")
    data = dict(SESSION)
    data["systemCode"] = body.get("systemCode") or "middleware"
    return ok(data)


@app.post("/api/v1/sso/validate")
def sso_validate():
    body = request.get_json(silent=True) or {}
    if body.get("tssotoken") != "mock-token":
        return fail("mock: invalid tssotoken")
    return ok(USER)


@app.get("/api/v1/sso/user/<login_name>")
def sso_user(login_name):
    data = dict(USER)
    data["loginName"] = login_name
    return ok(data)


@app.get("/api/v1/msp/users/by-login/<login_name>")
def msp_user(login_name):
    data = dict(USER)
    data["loginName"] = login_name
    return ok(data)


@app.get("/api/v1/sso/roles/<login_name>")
def sso_roles(login_name):
    return ok(ROLES)


@app.get("/api/v1/sso/org/<login_name>")
def sso_org(login_name):
    return ok({"orgCode": USER["orgCode"], "orgName": USER["orgName"],
               "parentOrgCode": "ORG000"})


# ---------------------------------------------------------------- msp

@app.get("/api/v1/msp/user/<login_name>/rights")
def msp_rights(login_name):
    return ok(RIGHTS)


@app.get("/api/v1/msp/user/<login_name>/rights/<right_code>")
def msp_right_check(login_name, right_code):
    return ok({"userCode": login_name, "rightsCode": right_code,
               "hasRight": right_code in RIGHTS})


@app.get("/api/v1/msp/user/<login_name>/menus")
def msp_menus(login_name):
    return ok(MENUS)


@app.get("/api/v1/msp/systems")
def msp_systems():
    return ok([
        {"systemId": "S001", "systemCode": "middleware",
         "systemName": "middleware 中间件", "brief": "middleware", "state": 1},
        {"systemId": "S002", "systemCode": "huizhi",
         "systemName": "huizhi-assistant", "brief": "huizhi", "state": 1},
    ])


@app.get("/api/v1/msp/orgs")
def msp_orgs():
    return ok([
        {"orgCode": "ORG001", "orgName": "测试部门", "parentOrgCode": "ORG000"},
        {"orgCode": "ORG002", "orgName": "测试子部门", "parentOrgCode": "ORG001"},
    ])


# ---------------------------------------------------------------- tcms

@app.get("/api/v1/tcms/user/<user_id>/cases")
def tcms_user_cases(user_id):
    case_kind = request.args.get("caseKind")
    case_status = request.args.get("caseStatus")
    result = CASES
    if case_kind:
        result = [c for c in result if c.get("caseKindId") == case_kind]
    if case_status:
        result = [c for c in result if c.get("status") == case_status]
    return ok(result)


@app.get("/api/v1/tcms/case/<case_code>")
def tcms_case_by_code(case_code):
    for item in CASES:
        if item["caseCode"] == case_code:
            return ok(item)
    return fail("案件不存在：%s" % case_code)


@app.get("/api/v1/tcms/cases/id/<case_id>")
def tcms_case_by_id(case_id):
    for item in CASES:
        if item["caseId"] == case_id:
            return ok(item)
    return fail("案件不存在：%s" % case_id)


@app.get("/api/v1/tcms/cases/<case_id>/archives")
def tcms_archives(case_id):
    kind = request.args.get("archiveKindCode")
    case = next((c for c in CASES if c["caseId"] == case_id), None)
    if not case:
        return fail("案件不存在：%s" % case_id)
    result = [a for a in ARCHIVES if a["caseCode"] == case["caseCode"]]
    if kind:
        result = [a for a in result if a["archiveKindCode"] == kind]
    return ok(result)


@app.get("/api/v1/tcms/cases/<case_id>/archives/approved")
def tcms_archives_approved(case_id):
    case = next((c for c in CASES if c["caseId"] == case_id), None)
    if not case:
        return fail("案件不存在：%s" % case_id)
    return ok([a for a in ARCHIVES
               if a["caseCode"] == case["caseCode"] and a["archiveStatus"] == "2"])


@app.get("/api/v1/tcms/cases/<case_id>/archives/<archive_code>")
def tcms_archive_by_code(case_id, archive_code):
    for item in ARCHIVES:
        if item["archiveCode"] == archive_code:
            return ok(item)
    return fail("档案不存在：%s" % archive_code)


@app.get("/api/v1/tcms/cases/<case_id>/targets")
def tcms_targets(case_id):
    return ok([t for t in TARGETS if t["caseId"] == case_id])


@app.get("/api/v1/tcms/cases/<case_id>/org")
def tcms_org_cases(case_id):
    return ok(CASES)


@app.get("/api/v1/tcms/flow/<task_id>/status")
def tcms_flow_status(task_id):
    if task_id in FLOWS:
        return ok(FLOWS[task_id])
    return fail("流程不存在，taskId=%s" % task_id)


@app.get("/api/v1/tcms/flow/<flow_id>/archive")
def tcms_flow_archive(flow_id):
    for item in ARCHIVES:
        if item["archiveCode"] == flow_id:
            return ok(item)
    return fail("流程关联档案不存在：%s" % flow_id)


@app.post("/api/v1/tcms/flow/start")
def tcms_flow_start():
    body = request.get_json(silent=True) or {}
    return fail("mock: TCMS 流程发起未实现（body=%s）" % body.get("taskId"))


@app.post("/api/v1/tcms/flow/start-common")
def tcms_flow_start_common():
    return fail("mock: TCMS 通用流程发起未实现")


@app.put("/api/v1/tcms/flow/<archive_code>/status")
def tcms_flow_update(archive_code):
    body = request.get_json(silent=True) or {}
    return ok({"flowId": archive_code, "status": body.get("status"),
               "statusName": "mock"})


# ---------------------------------------------------------------- elc

@app.post("/api/v1/elc/log")
def elc_log():
    body = request.get_json(silent=True) or {}
    log_id = "LOG-%04d" % (len(ELC_LOGS) + 1)
    ELC_LOGS.append(body)
    return ok({"logId": log_id, "status": "SUCCESS", "message": "日志写入成功"})


@app.post("/api/v1/elc/log/login")
def elc_login():
    ELC_LOGS.append({"loginName": request.args.get("loginName"), "module": "CK"})
    return ok({"logId": "LOGIN-%04d" % len(ELC_LOGS), "status": "SUCCESS",
               "message": "登录日志写入成功"})


@app.post("/api/v1/elc/log/operation")
def elc_operation():
    ELC_LOGS.append(dict(request.args))
    return ok({"logId": "OP-%04d" % len(ELC_LOGS), "status": "SUCCESS",
               "message": "操作日志写入成功"})


@app.post("/api/v1/elc/logs/batch")
def elc_batch():
    body = request.get_json(silent=True) or []
    if not isinstance(body, list):
        return fail("要求 JSON 数组")
    ELC_LOGS.extend(body)
    return ok({"successCount": len(body)})


@app.get("/__mock/elc/logs")
def mock_elc_logs():
    return jsonify({"count": len(ELC_LOGS), "logs": ELC_LOGS})


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok", "service": "mock-middleware"})


if __name__ == "__main__":
    print("mock middleware listening on http://127.0.0.1:19089")
    app.run(host="0.0.0.0", port=19089, debug=False, threaded=True)
