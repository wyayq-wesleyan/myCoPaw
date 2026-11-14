"""MSP 平台模块。"""

from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..config import DEFAULT_SYSTEM_CODE
from ..http import as_dict, as_list, data, message, ok
from ..services import (
    current_login_name,
    msp_menus,
    msp_org,
    msp_orgs,
    msp_right_check,
    msp_rights,
    msp_roles,
    msp_systems,
    msp_user,
)
from ..store import get_state

bp = Blueprint("msp", __name__, url_prefix="/msp")


def _need_login():
    state = get_state()
    login_name = current_login_name(state)
    if not login_name:
        flash("请先在“统一认证”建立会话，或按 loginName 加载", "warning")
        return None, None
    return state, login_name


def build_menu_tree(menus):
    nodes, indexed = [], {}
    for menu in menus:
        if not isinstance(menu, dict):
            continue
        node = dict(menu)
        node["children"] = []
        key = str(menu.get("menuId") or menu.get("menuCode") or "")
        nodes.append(node)
        if key:
            indexed[key] = node
    roots = []
    for node in nodes:
        parent = node.get("parentCode")
        parent_key = str(parent) if parent not in (None, "", "0", "-1") else ""
        if parent_key and parent_key in indexed and indexed[parent_key] is not node:
            indexed[parent_key]["children"].append(node)
        else:
            roots.append(node)

    def sort_nodes(items):
        items.sort(key=lambda item: (
            item.get("orderNum") is None,
            item.get("orderNum") or 0,
            item.get("menuName") or "",
        ))
        for item in items:
            sort_nodes(item["children"])

    sort_nodes(roots)
    return roots


@bp.route("/user")
def user():
    state, login_name = _need_login()
    if not state:
        return redirect(url_for("auth.login"))
    response = msp_user(state, login_name)
    payload = as_dict(data(response, {}))
    if payload:
        current = dict(state.get("user") or {})
        current.update({k: v for k, v in payload.items() if v is not None})
        state["user"] = current
    return render_template("msp/user.html", response=response, user=payload)


@bp.route("/rights")
def rights():
    state, login_name = _need_login()
    if not state:
        return redirect(url_for("auth.login"))
    response = msp_rights(state, login_name)
    payload = as_list(data(response, []))
    if payload:
        state["rights"] = payload
    return render_template("msp/rights.html", response=response, rights=payload,
                           check_result=None)


@bp.route("/rights/check", methods=["POST"])
def rights_check():
    state, login_name = _need_login()
    if not state:
        return redirect(url_for("auth.login"))
    right_code = (request.form.get("right_code") or "").strip()
    response = msp_right_check(state, login_name, right_code) if right_code else None
    listing = msp_rights(state, login_name)
    payload = as_list(data(listing, []))
    return render_template(
        "msp/rights.html", response=response, rights=payload,
        check_result=as_dict(data(response, {})), right_code=right_code,
    )


@bp.route("/menus")
def menus():
    state, login_name = _need_login()
    if not state:
        return redirect(url_for("auth.login"))
    sys_code = (request.args.get("systemCode") or state.get("system_code")
                or DEFAULT_SYSTEM_CODE)
    response = msp_menus(state, login_name, sys_code)
    payload = as_list(data(response, []))
    if payload:
        state["menus"] = payload
    return render_template("msp/menus.html", response=response, menus=payload,
                           menu_tree=build_menu_tree(payload), system_code_value=sys_code)


@bp.route("/roles")
def roles():
    state, login_name = _need_login()
    if not state:
        return redirect(url_for("auth.login"))
    response = msp_roles(state, login_name)
    return render_template("msp/list.html", title="用户角色",
                           subtitle="GET /api/v1/sso/roles/{loginName}",
                           response=response, data=as_list(data(response, [])))


@bp.route("/org")
def org():
    state, login_name = _need_login()
    if not state:
        return redirect(url_for("auth.login"))
    response = msp_org(state, login_name)
    return render_template("msp/list.html", title="用户组织",
                           subtitle="GET /api/v1/sso/org/{loginName}",
                           response=response, data=as_dict(data(response, {})))


@bp.route("/systems")
def systems():
    state = get_state()
    response = msp_systems(state)
    return render_template("msp/list.html", title="MSP 系统列表",
                           subtitle="GET /api/v1/msp/systems",
                           response=response, data=as_list(data(response, [])))


@bp.route("/orgs")
def orgs():
    state = get_state()
    response = msp_orgs(state)
    return render_template("msp/list.html", title="MSP 组织列表",
                           subtitle="GET /api/v1/msp/orgs",
                           response=response, data=as_list(data(response, [])))
