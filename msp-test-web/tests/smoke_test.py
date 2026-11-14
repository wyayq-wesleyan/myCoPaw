#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端冒烟测试：先启动 mock_middleware.py 和 app.py。"""

import sys

import requests

APP = "http://127.0.0.1:18080"
MOCK = "http://127.0.0.1:19089"


def check(condition, message):
    if not condition:
        print("FAIL: " + message)
        raise SystemExit(1)
    print("ok: " + message)


def main():
    session = requests.Session()

    # 1. 配置
    response = session.post(
        APP + "/settings/",
        data={
            "base_url": MOCK,
            "system_code": "middleware",
            "tcms_system_code": "middleware",
            "elc_system_code": "middleware",
            "sso_logout_url": "",
        },
        timeout=10,
        allow_redirects=True,
    )
    check(response.status_code == 200, "settings saved")

    # 2. 首页
    response = session.get(APP + "/", timeout=10)
    check("联调测试台" in response.text, "dashboard renders")
    check("TCMS 案管" in response.text, "dashboard shows TCMS module card")
    check("ELC 审计" in response.text, "dashboard shows ELC module card")

    # 3. SSO 跳转 + 回调
    response = session.post(
        APP + "/auth/login/redirect",
        data={"target_url": APP + "/auth/callback", "system_code": "middleware"},
        timeout=10,
        allow_redirects=True,
    )
    check("张三" in response.text, "login redirect + callback completes")

    # 4. MSP
    response = session.get(APP + "/msp/rights", timeout=10)
    check("case:query" in response.text, "msp rights shows case:query")

    response = session.get(APP + "/msp/menus", timeout=10)
    check("案件查询" in response.text, "msp menus shows 案件查询")

    response = session.get(APP + "/msp/user", timeout=10)
    check("张三" in response.text, "msp user shows 张三")

    response = session.get(APP + "/msp/systems", timeout=10)
    check("middleware 中间件" in response.text, "msp systems loads")

    # 5. TCMS 案件
    response = session.get(APP + "/tcms/cases", timeout=10)
    check("测试刑事案件一号" in response.text, "tcms cases lists case 1")
    check("测试情报案件二号" in response.text, "tcms cases lists case 2")

    response = session.get(
        APP + "/tcms/cases",
        params={"userId": "U1001", "caseKind": "caseKind.28"},
        timeout=10,
    )
    check("测试刑事案件一号" in response.text, "tcms cases filter by caseKind")
    check("测试情报案件二号" not in response.text, "tcms caseKind filter excludes others")

    response = session.get(
        APP + "/tcms/case", params={"caseCode": "A202600001"}, timeout=10)
    check("测试刑事案件一号" in response.text, "tcms case detail by caseCode")
    check("CASE-2026-0001" in response.text, "tcms case detail shows caseId")

    response = session.get(
        APP + "/tcms/archives", params={"caseId": "CASE-2026-0001"}, timeout=10)
    check("数据查询审批表" in response.text, "tcms archives lists archive")
    check("ARC-20260101-001" in response.text, "tcms archives shows archiveCode")

    response = session.get(
        APP + "/tcms/archives",
        params={"caseId": "CASE-2026-0001", "approved": "1"},
        timeout=10,
    )
    check("数据查询审批表" in response.text, "tcms approved archives lists approved")
    check("数据分析审批表" not in response.text, "approved filter excludes pending")

    response = session.get(
        APP + "/tcms/archives/detail",
        params={"caseId": "CASE-2026-0001", "archiveCode": "ARC-20260101-001"},
        timeout=10,
    )
    check("数据查询审批表" in response.text, "tcms archive detail renders")

    response = session.get(
        APP + "/tcms/targets",
        params={"caseId": "CASE-2026-0001", "userId": "U1001"},
        timeout=10,
    )
    check("嫌疑人甲" in response.text, "tcms targets lists target")
    check("13800000001" in response.text, "tcms targets shows number")

    response = session.get(
        APP + "/tcms/org-cases",
        params={"orgId": "ORG-001", "userId": "U1001"},
        timeout=10,
    )
    check("测试刑事案件一号" in response.text, "tcms org cases renders")

    # 6. TCMS 流程
    response = session.post(
        APP + "/tcms/flows", data={"action": "status", "task_id": "TASK-001"},
        timeout=10,
    )
    check("审批完成" in response.text, "tcms flow status renders")

    response = session.post(
        APP + "/tcms/flows", data={"action": "update", "archive_code": "ARC-1",
                                   "status": "0"}, timeout=10)
    check(response.status_code == 200, "tcms flow update handled")

    # 7. ELC
    response = session.post(
        APP + "/elc/log",
        data={
            "module": "查询类", "operation_type": "数据查询",
            "operation_desc": "查询案件列表", "login_name": "zhangsan",
            "client_ip": "192.168.1.10", "request_url": "/api/v1/tcms/user/cases",
            "request_method": "GET", "request_params": "{}", "result": "SUCCESS",
            "cost_time": "120",
        },
        timeout=10,
    )
    check("LOG-0001" in response.text, "elc single log written")

    response = session.post(
        APP + "/elc/login-log", data={"login_name": "zhangsan"}, timeout=10)
    check("LOGIN-" in response.text, "elc login log written")

    response = session.post(
        APP + "/elc/operation-log",
        data={
            "login_name": "zhangsan", "module": "查询类",
            "operation_type": "数据查询", "operation_desc": "查询用户列表",
            "request_url": "/api/v1/msp/users",
        },
        timeout=10,
    )
    check("OP-" in response.text, "elc operation log written")

    response = session.post(
        APP + "/elc/batch",
        data={"body": '[{"module":"查询类","operationType":"x","operationDesc":"y",'
                      '"loginName":"zhangsan","requestUrl":"/z","result":"SUCCESS"}]'},
        timeout=10,
    )
    check("successCount" in response.text, "elc batch log written")

    mock_logs = requests.get(MOCK + "/__mock/elc/logs", timeout=10).json()
    check(mock_logs["count"] >= 4, "mock ELC received all logs")

    # 8. 原始 API + 预设
    response = session.get(APP + "/tools/raw", timeout=10)
    check("原始 API 调试台" in response.text, "raw console renders")
    check("用户案件" in response.text, "raw console has TCMS preset")

    response = session.post(
        APP + "/tools/raw",
        data={"method": "GET", "path": "/api/v1/tcms/case/A202600001",
              "query": "", "body": ""},
        timeout=10,
    )
    check("测试刑事案件一号" in response.text, "raw console calls TCMS case")

    # 9. 会话调试
    payload = session.get(APP + "/session", timeout=10).json()
    check(payload.get("logged_in") is True, "session shows logged_in=true")
    check(payload.get("user", {}).get("userId") == "U1001", "session has userId")

    # 10. 登出
    response = session.post(APP + "/auth/logout", data={}, timeout=10,
                            allow_redirects=True)
    check("本地会话已清理" in response.text or "未登录" in response.text,
          "logout clears session")
    payload = session.get(APP + "/session", timeout=10).json()
    check(payload.get("logged_in") is False, "session shows logged_in=false")

    print("\nSMOKE TEST PASS")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as exc:
        print("REQUEST ERROR: %s" % exc)
        print("请确认 mock_middleware.py 和 app.py 都已启动。")
        sys.exit(1)
