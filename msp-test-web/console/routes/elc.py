"""ELC 审计日志模块。"""

from flask import Blueprint, flash, render_template, request

from ..http import as_dict, data, message, ok
from ..services import (
    current_login_name,
    elc_batch,
    elc_log,
    elc_login_log,
    elc_operation_log,
)
from ..store import get_state

bp = Blueprint("elc", __name__, url_prefix="/elc")


@bp.route("/log", methods=["GET", "POST"])
def log():
    state = get_state()
    response = None
    log_id = None
    if request.method == "POST":
        payload = {
            "module": (request.form.get("module") or "").strip(),
            "operationType": (request.form.get("operation_type") or "").strip(),
            "operationDesc": (request.form.get("operation_desc") or "").strip(),
            "loginName": (request.form.get("login_name")
                          or current_login_name(state) or "").strip(),
            "clientIp": (request.form.get("client_ip") or "").strip(),
            "requestUrl": (request.form.get("request_url") or "").strip(),
            "requestMethod": (request.form.get("request_method") or "POST").strip(),
            "requestParams": (request.form.get("request_params") or "").strip(),
            "result": (request.form.get("result") or "SUCCESS").strip(),
        }
        cost = (request.form.get("cost_time") or "").strip()
        if cost:
            try:
                payload["costTime"] = int(cost)
            except ValueError:
                flash("costTime 必须是整数", "warning")
        response = elc_log(state, payload)
        log_id = as_dict(data(response, {})).get("logId") if ok(response) else None
        flash("日志写入成功：%s" % (log_id or "(无 logId)")
              if ok(response) else "日志写入失败：%s" % message(response),
              "success" if ok(response) else "error")

    return render_template(
        "elc/log.html", response=response, log_id=log_id,
        login_name=current_login_name(state), default_module="查询类",
    )


@bp.route("/login-log", methods=["GET", "POST"])
def login_log():
    state = get_state()
    response = None
    if request.method == "POST":
        login_name = (request.form.get("login_name")
                      or current_login_name(state) or "").strip()
        client_ip = (request.form.get("client_ip") or "").strip()
        if not login_name:
            flash("loginName 不能为空", "error")
        else:
            response = elc_login_log(state, login_name, client_ip or None)
            flash("登录日志写入成功" if ok(response)
                  else "登录日志写入失败：%s" % message(response),
                  "success" if ok(response) else "error")
    return render_template("elc/login_log.html", response=response,
                           login_name=current_login_name(state))


@bp.route("/operation-log", methods=["GET", "POST"])
def operation_log():
    state = get_state()
    response = None
    if request.method == "POST":
        params = {
            "loginName": (request.form.get("login_name")
                          or current_login_name(state) or "").strip(),
            "module": (request.form.get("module") or "").strip(),
            "operationType": (request.form.get("operation_type") or "").strip(),
            "operationDesc": (request.form.get("operation_desc") or "").strip(),
            "requestUrl": (request.form.get("request_url") or "").strip(),
        }
        cost = (request.form.get("cost_time") or "").strip()
        if cost:
            try:
                params["costTime"] = int(cost)
            except ValueError:
                flash("costTime 必须是整数", "warning")

        missing = [key for key in
                   ("loginName", "module", "operationType", "operationDesc", "requestUrl")
                   if not params.get(key)]
        if missing:
            flash("以下参数必填：%s" % "、".join(missing), "error")
        else:
            response = elc_operation_log(state, params)
            flash("操作日志写入成功" if ok(response)
                  else "操作日志写入失败：%s" % message(response),
                  "success" if ok(response) else "error")

    return render_template("elc/operation_log.html", response=response,
                           login_name=current_login_name(state))


@bp.route("/batch", methods=["GET", "POST"])
def batch():
    state = get_state()
    response = None
    success_count = None
    body = ""
    if request.method == "POST":
        body = request.form.get("body") or ""
        import json
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            flash("请求体不是合法 JSON：%s" % exc, "error")
            payload = None
        if payload is not None:
            if not isinstance(payload, list):
                flash("批量接口要求 JSON 数组", "error")
            else:
                response = elc_batch(state, payload)
                if ok(response):
                    success_count = as_dict(data(response, {})).get("successCount")
                    flash("批量写入成功，successCount=%s" % success_count, "success")
                else:
                    flash("批量写入失败：%s" % message(response), "error")

    return render_template(
        "elc/batch.html", response=response, success_count=success_count, body=body,
        login_name=current_login_name(state),
    )
