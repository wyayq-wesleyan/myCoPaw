"""统一认证：SSO 跳转、token 会话、登出。"""

from urllib.parse import quote

from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..config import DEFAULT_SYSTEM_CODE
from ..http import data, first_value, message, ok
from ..services import (
    current_login_name,
    fetch_user_bundle,
    login_by_username,
    login_url,
    login_with_token,
    validate_token,
)
from ..store import clear_login, get_state, mask_token

bp = Blueprint("auth", __name__, url_prefix="/auth")


def _callback_url():
    return url_for("auth.callback", _external=True)


@bp.route("/login")
def login():
    state = get_state()
    return render_template(
        "auth/login.html",
        callback_url=_callback_url(),
        login_url=state.get("last_login_url", ""),
        response=state.get("last_login_response"),
        bundle=None,
        system_code=state.get("system_code") or DEFAULT_SYSTEM_CODE,
    )


@bp.route("/login/url", methods=["POST"])
def login_url_only():
    state = get_state()
    sys_code = (request.form.get("system_code") or "").strip() or state.get(
        "system_code") or DEFAULT_SYSTEM_CODE
    target_url = (request.form.get("target_url") or "").strip() or _callback_url()
    state["system_code"] = sys_code
    state["pending_login"] = {"target_url": target_url, "system_code": sys_code}
    response = login_url(state, target_url)
    state["last_login_response"] = response
    if ok(response):
        state["last_login_url"] = data(response, "") or ""
        flash("已获取 SSO 登录 URL，可以先检查再跳转", "success")
    else:
        flash("获取登录 URL 失败：%s" % message(response), "error")
    return render_template(
        "auth/login.html",
        callback_url=target_url,
        login_url=state.get("last_login_url", ""),
        response=response,
        bundle=None,
        system_code=sys_code,
    )


@bp.route("/login/redirect", methods=["POST"])
def login_redirect():
    state = get_state()
    sys_code = (request.form.get("system_code") or "").strip() or state.get(
        "system_code") or DEFAULT_SYSTEM_CODE
    target_url = (request.form.get("target_url") or "").strip() or _callback_url()
    login_url_value = (request.form.get("login_url") or "").strip()
    state["system_code"] = sys_code
    state["pending_login"] = {"target_url": target_url, "system_code": sys_code}

    if not login_url_value:
        response = login_url(state, target_url)
        state["last_login_response"] = response
        if not ok(response):
            flash("获取登录 URL 失败：%s" % message(response), "error")
            return render_template(
                "auth/login.html",
                callback_url=target_url,
                login_url="",
                response=response,
                bundle=None,
                system_code=sys_code,
            )
        login_url_value = data(response, "") or ""
        state["last_login_url"] = login_url_value

    if not login_url_value:
        flash("登录 URL 为空，无法跳转", "error")
        return redirect(url_for("auth.login"))
    return redirect(login_url_value)


@bp.route("/login/token", methods=["POST"])
def login_token():
    state = get_state()
    token = (request.form.get("tssotoken") or "").strip()
    sid = (request.form.get("session_id") or "").strip()
    sys_code = (request.form.get("system_code") or "").strip() or state.get(
        "system_code") or DEFAULT_SYSTEM_CODE
    state["system_code"] = sys_code

    if not token:
        flash("tssotoken 不能为空", "error")
        return redirect(url_for("auth.login"))

    response = login_with_token(state, token, sid, sys_code)
    if ok(response):
        flash("Token 校验成功，平台会话已建立", "success")
        return redirect(url_for("main.index"))
    flash("Token 登录失败：%s" % message(response), "error")
    return render_template(
        "auth/login.html",
        callback_url=_callback_url(),
        login_url=state.get("last_login_url", ""),
        response=response,
        bundle=None,
        system_code=sys_code,
    )


@bp.route("/login/validate", methods=["POST"])
def login_validate():
    state = get_state()
    token = (request.form.get("tssotoken") or "").strip()
    sid = (request.form.get("session_id") or "").strip()
    response = validate_token(state, token, sid) if token else None
    if response is None:
        flash("tssotoken 不能为空", "error")
    elif ok(response):
        flash("token 校验通过（只调 /sso/validate，未建立平台会话）", "success")
    else:
        flash("token 校验失败：%s" % message(response), "error")
    return render_template(
        "auth/login.html",
        callback_url=_callback_url(),
        login_url=state.get("last_login_url", ""),
        response=response,
        bundle=None,
        system_code=state.get("system_code") or DEFAULT_SYSTEM_CODE,
    )


@bp.route("/login/username", methods=["POST"])
def login_username():
    state = get_state()
    login_name = (request.form.get("login_name") or "").strip()
    sys_code = (request.form.get("system_code") or "").strip() or state.get(
        "system_code") or DEFAULT_SYSTEM_CODE
    state["system_code"] = sys_code
    if not login_name:
        flash("loginName 不能为空", "error")
        return redirect(url_for("auth.login"))

    bundle = login_by_username(state, login_name, sys_code)
    if all(ok(item) for item in bundle["responses"]):
        flash("已按 loginName 加载用户、权限和菜单", "success")
    else:
        flash("已加载，但部分 MSP 接口失败，详见原始响应", "warning")
    return render_template(
        "auth/login.html",
        callback_url=_callback_url(),
        login_url=state.get("last_login_url", ""),
        response=None,
        bundle=bundle,
        system_code=sys_code,
    )


@bp.route("/callback")
def callback():
    state = get_state()
    params = request.args.to_dict(flat=True)
    token = first_value(params, ("tssotoken", "token", "TSSOTOKEN"))
    username = first_value(params, ("username", "userName", "loginName", "user", "login_name"))
    sid = first_value(params, ("sessionId", "session_id", "sId", "sid"))
    pending = state.get("pending_login") or {}
    sys_code = (
        first_value(params, ("systemCode", "system_code"))
        or pending.get("system_code")
        or state.get("system_code")
        or DEFAULT_SYSTEM_CODE
    )

    shown = {
        key: (mask_token(value) if "token" in key.lower() else value)
        for key, value in params.items()
    }
    state["last_callback"] = {"params": shown, "received_at": _now()}

    if token:
        response = login_with_token(state, token, sid, sys_code)
        if ok(response):
            flash("SSO 回调成功，平台会话已建立", "success")
            return render_template("auth/callback.html", success=True, params=shown,
                                   response=response, bundle=None,
                                   login_name=current_login_name(state),
                                   message=None)
        return render_template(
            "auth/callback.html", success=False, params=shown, response=response,
            bundle=None, login_name=None,
            message="收到 tssotoken，但 /api/v1/platform/session 校验失败：%s" % message(response),
        ), 502

    if username:
        bundle = fetch_user_bundle(state, username, sys_code)
        from ..services import apply_session
        apply_session(state, bundle["data"], "SSO 回调 username（无 token）")
        return render_template(
            "auth/callback.html", success=True, params=shown, response=None,
            bundle=bundle, login_name=username,
            message="回调没有 tssotoken，仅按 username=%s 查询用户/权限/菜单；"
                    "这不是完整的 SSO 登录校验。" % username,
        )

    return render_template(
        "auth/callback.html", success=False, params=shown, response=None,
        bundle=None, login_name=None,
        message="回调参数里既没有 tssotoken，也没有 username；请检查 SSO 登录页的 targetUrl 拼接。",
    ), 400


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    state = get_state()
    want_sso = request.values.get("sso") == "1"
    clear_login(state)
    flash("本地会话已清理", "success")
    logout_url = (state.get("sso_logout_url") or "").strip()
    if want_sso and logout_url:
        return redirect(logout_url)
    if want_sso and not logout_url:
        flash("未配置 SSO 登出地址，只清理了本地会话", "warning")
    return redirect(url_for("main.index"))


def _now():
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")
