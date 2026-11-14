"""首页与模块总览。"""

from flask import Blueprint, render_template

from ..config import MODULES
from ..services import current_login_name
from ..store import get_state

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    state = get_state()
    return render_template(
        "index.html",
        modules=MODULES,
        logged_in=bool(current_login_name(state)),
        user=state.get("user") or {},
        roles=state.get("roles") or [],
        menus=state.get("menus") or [],
        rights=state.get("rights") or [],
        login_at=state.get("login_at"),
        login_source=state.get("login_source"),
        tssotoken=state.get("tssotoken"),
        session_payload=state.get("session_payload"),
        last_case=state.get("last_case"),
    )
