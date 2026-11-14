"""连接与模块设置。"""

from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..config import (
    DEFAULT_SYSTEM_CODE,
    DEFAULT_TCMS_SYSTEM_CODE,
    DEFAULT_ELC_SYSTEM_CODE,
    MODULE_MAP,
)
from ..store import clear_login, get_state

bp = Blueprint("settings", __name__, url_prefix="/settings")


@bp.route("/", methods=["GET", "POST"])
def index():
    state = get_state()
    if request.method == "POST":
        new_base = (request.form.get("base_url") or "").strip().rstrip("/")
        new_system = (request.form.get("system_code") or "").strip() or DEFAULT_SYSTEM_CODE
        new_tcms = (request.form.get("tcms_system_code") or "").strip() \
            or DEFAULT_TCMS_SYSTEM_CODE
        new_elc = (request.form.get("elc_system_code") or "").strip() \
            or DEFAULT_ELC_SYSTEM_CODE
        new_logout = (request.form.get("sso_logout_url") or "").strip()

        if not new_base:
            flash("middleware 地址不能为空", "error")
        else:
            changed = (
                new_base != state.get("base_url")
                or new_system != state.get("system_code")
            )
            state["base_url"] = new_base
            state["system_code"] = new_system
            state["tcms_system_code"] = new_tcms
            state["elc_system_code"] = new_elc
            state["sso_logout_url"] = new_logout
            if changed:
                clear_login(state)
            flash("配置已保存", "success")
            return redirect(url_for("main.index"))

    return render_template("settings.html", module_map=MODULE_MAP)
