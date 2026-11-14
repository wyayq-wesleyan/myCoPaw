"""TCMS 案管模块：案件、档案、目标对象、流程。"""

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from ..config import DEFAULT_TCMS_SYSTEM_CODE
from ..http import as_dict, as_list, data, message, ok
from ..services import (
    current_login_name,
    current_user_id,
    tcms_approved_archives,
    tcms_archive_by_code,
    tcms_archives,
    tcms_case_by_code,
    tcms_case_by_id,
    tcms_flow_archive,
    tcms_flow_status,
    tcms_org_cases,
    tcms_start_common,
    tcms_start_flow,
    tcms_targets,
    tcms_update_status,
    tcms_user_cases,
)
from ..store import get_state

bp = Blueprint("tcms", __name__, url_prefix="/tcms")


def _context():
    state = get_state()
    return state, current_login_name(state), current_user_id(state)


def _tcms_code(state):
    return state.get("tcms_system_code") or state.get("system_code") \
        or DEFAULT_TCMS_SYSTEM_CODE


@bp.route("/cases")
def cases():
    state, login_name, user_id = _context()
    if not login_name:
        flash("请先在“统一认证”建立会话", "warning")
        return redirect(url_for("auth.login"))

    user_id = (request.args.get("userId") or user_id or "").strip()
    case_kind = (request.args.get("caseKind") or "").strip()
    case_status = (request.args.get("caseStatus") or "").strip()

    response = None
    payload = as_list(state.get("cases"))
    if user_id:
        response = tcms_user_cases(state, user_id, case_kind or None, case_status or None)
        if ok(response):
            payload = as_list(data(response, []))
            state["cases"] = payload
            state["last_case_query"] = {
                "userId": user_id,
                "caseKind": case_kind,
                "caseStatus": case_status,
            }
    else:
        flash("当前会话没有 userId，请先加载用户信息或手工填写 userId", "warning")

    return render_template(
        "tcms/cases.html", response=response, cases=payload, user_id=user_id,
        case_kind=case_kind, case_status=case_status, login_name=login_name,
    )


@bp.route("/case")
def case_detail():
    state, login_name, user_id = _context()
    if not login_name:
        flash("请先在“统一认证”建立会话", "warning")
        return redirect(url_for("auth.login"))

    case_code = (request.args.get("caseCode") or "").strip()
    case_id = (request.args.get("caseId") or "").strip()
    if not case_code and not case_id:
        flash("请填写 caseCode 或 caseId", "warning")
        return redirect(url_for("tcms.cases"))

    if case_code:
        response = tcms_case_by_code(state, case_code)
        query = {"caseCode": case_code}
    else:
        response = tcms_case_by_id(state, case_id)
        query = {"caseId": case_id}

    payload = as_dict(data(response, {}))
    if payload:
        state["last_case"] = payload
        if payload.get("caseId"):
            state["last_case_id"] = payload["caseId"]
    return render_template(
        "tcms/case_detail.html", response=response, case=payload, query=query,
        login_name=login_name,
    )


@bp.route("/archives")
def archives():
    state, login_name, user_id = _context()
    if not login_name:
        flash("请先在“统一认证”建立会话", "warning")
        return redirect(url_for("auth.login"))

    case_id = (request.args.get("caseId") or state.get("last_case_id") or "").strip()
    sys_code = (request.args.get("systemCode") or _tcms_code(state)).strip()
    kind_code = (request.args.get("archiveKindCode") or "").strip()
    approved_only = request.args.get("approved") == "1"

    response, payload = None, []
    if case_id:
        if approved_only:
            response = tcms_approved_archives(state, case_id, sys_code, kind_code or None)
        else:
            response = tcms_archives(state, case_id, sys_code, kind_code or None)
        if ok(response):
            payload = as_list(data(response, []))
    else:
        payload = as_list(state.get("last_archives"))

    return render_template(
        "tcms/archives.html", response=response, archives=payload, case_id=case_id,
        system_code_value=sys_code, kind_code=kind_code, approved_only=approved_only,
        login_name=login_name,
    )


@bp.route("/archives/detail")
def archive_detail():
    state, login_name, user_id = _context()
    if not login_name:
        flash("请先在“统一认证”建立会话", "warning")
        return redirect(url_for("auth.login"))

    case_id = (request.args.get("caseId") or "").strip()
    archive_code = (request.args.get("archiveCode") or "").strip()
    sys_code = (request.args.get("systemCode") or _tcms_code(state)).strip()
    kind_code = (request.args.get("archiveKindCode") or "").strip()

    response = None
    if case_id and archive_code:
        response = tcms_archive_by_code(state, case_id, archive_code, sys_code,
                                        kind_code or None)
    else:
        flash("需要 caseId 和 archiveCode", "warning")

    return render_template(
        "tcms/archive_detail.html", response=response,
        archive=as_dict(data(response, {})), case_id=case_id,
        archive_code=archive_code, system_code_value=sys_code, kind_code=kind_code,
    )


@bp.route("/targets")
def targets():
    state, login_name, user_id = _context()
    if not login_name:
        flash("请先在“统一认证”建立会话", "warning")
        return redirect(url_for("auth.login"))

    case_id = (request.args.get("caseId") or state.get("last_case_id") or "").strip()
    user_id = (request.args.get("userId") or user_id or "").strip()
    response, payload = None, []
    if case_id and user_id:
        response = tcms_targets(state, case_id, user_id)
        if ok(response):
            payload = as_list(data(response, []))
    elif case_id:
        flash("需要 userId 才能查询目标对象", "warning")

    return render_template(
        "tcms/targets.html", response=response, targets=payload, case_id=case_id,
        user_id=user_id, login_name=login_name,
    )


@bp.route("/org-cases")
def org_cases():
    state, login_name, user_id = _context()
    if not login_name:
        flash("请先在“统一认证”建立会话", "warning")
        return redirect(url_for("auth.login"))

    org_id = (request.args.get("orgId") or as_dict(state.get("user")).get("orgId") or "").strip()
    user_id = (request.args.get("userId") or user_id or "").strip()
    status = (request.args.get("status") or "").strip()
    response, payload = None, []
    if org_id and user_id:
        response = tcms_org_cases(state, user_id, org_id, status or None)
        if ok(response):
            payload = as_list(data(response, []))

    return render_template(
        "tcms/org_cases.html", response=response, cases=payload, org_id=org_id,
        user_id=user_id, status=status, login_name=login_name,
    )


@bp.route("/flows", methods=["GET", "POST"])
def flows():
    state, login_name, user_id = _context()
    if not login_name:
        flash("请先在“统一认证”建立会话", "warning")
        return redirect(url_for("auth.login"))

    response = None
    result = None
    if request.method == "POST":
        action = (request.form.get("action") or "status").strip()
        if action == "status":
            task_id = (request.form.get("task_id") or "").strip()
            if task_id:
                response = tcms_flow_status(state, task_id)
                result = data(response, {})
            else:
                flash("taskId 不能为空", "error")
        elif action == "archive":
            flow_id = (request.form.get("flow_id") or "").strip()
            if flow_id:
                response = tcms_flow_archive(state, flow_id)
                result = data(response, {})
            else:
                flash("flowId 不能为空", "error")
        elif action == "update":
            archive_code = (request.form.get("archive_code") or "").strip()
            status = (request.form.get("status") or "").strip()
            if archive_code and status:
                response = tcms_update_status(state, archive_code, status)
                result = data(response, {})
            else:
                flash("archiveCode 和 status 都不能为空", "error")
        elif action == "start-common":
            payload = {
                "flowType": (request.form.get("flow_type") or "").strip(),
                "taskId": (request.form.get("task_id") or "").strip(),
                "reason": (request.form.get("reason") or "").strip(),
            }
            response = tcms_start_common(state, payload)
            result = data(response, {})
            if not ok(response):
                flash("发起通用流程失败：%s" % message(response), "error")
        elif action == "start":
            payload = {
                "loginName": (request.form.get("login_name") or login_name).strip(),
                "taskId": (request.form.get("task_id") or "").strip(),
                "flowType": (request.form.get("flow_type") or "").strip(),
                "reason": (request.form.get("reason") or "").strip(),
                "systemCode": (request.form.get("system_code") or _tcms_code(state)).strip(),
                "callbackUrl": (request.form.get("callback_url") or "").strip(),
            }
            response = tcms_start_flow(state, payload)
            result = data(response, {})
            if not ok(response):
                flash("发起流程失败：%s" % message(response), "error")

        if ok(response):
            flash("调用成功", "success")

    return render_template(
        "tcms/flows.html", response=response, result=result,
        login_name=login_name, user_id=user_id, tcms_system_code=_tcms_code(state),
        default_callback=url_for("tools.raw_api", _external=True),
    )
