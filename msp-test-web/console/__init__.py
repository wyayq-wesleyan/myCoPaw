"""Flask 应用工厂、模板辅助和路由注册。"""

import os

from flask import Flask, jsonify, render_template, request

from .config import MODULES, SECRET_KEY
from .services import current_login_name
from .store import get_state, mask_token, public_state


def create_app():
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.secret_key = SECRET_KEY
    app.config["JSON_AS_ASCII"] = False
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    if os.getenv("MSP_TEST_PROXY_FIX", "").lower() in {"1", "true", "yes"}:
        try:
            from werkzeug.middleware.proxy_fix import ProxyFix
            app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
        except Exception:
            pass

    register_filters(app)
    register_context(app)
    register_routes(app)
    return app


# ---------------------------------------------------------------------------
# 模板过滤器和上下文
# ---------------------------------------------------------------------------

def register_filters(app):
    from .http import mask_text, pretty

    @app.template_filter("prettyjson")
    def prettyjson(value):
        return pretty(value)

    @app.template_filter("masktoken")
    def masktoken(value):
        return mask_token(value)

    @app.template_filter("masktext")
    def masktext(value):
        return mask_text(value)

    @app.template_filter("collectionsize")
    def collectionsize(value):
        try:
            return len(value or [])
        except TypeError:
            return 0


def register_context(app):
    from .config import APP_TITLE

    @app.context_processor
    def inject_globals():
        state = get_state()
        module_key = (request.blueprint or "main")
        return {
            "app_title": APP_TITLE,
            "modules": MODULES,
            "active_module": module_key,
            "state": state,
            "logged_in": bool(current_login_name(state)),
            "login_name": current_login_name(state),
            "base_url": state.get("base_url"),
            "system_code": state.get("system_code"),
            "tcms_system_code": state.get("tcms_system_code"),
            "elc_system_code": state.get("elc_system_code"),
            "sso_logout_url": state.get("sso_logout_url"),
            "current_path": request.path,
        }


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

def register_routes(app):
    from .routes import auth, elc, home, msp, settings, tcms, tools

    app.register_blueprint(home.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(msp.bp)
    app.register_blueprint(tcms.bp)
    app.register_blueprint(elc.bp)
    app.register_blueprint(tools.bp)
    app.register_blueprint(settings.bp)

    @app.route("/healthz")
    def healthz():
        state = get_state()
        return jsonify({
            "status": "ok",
            "service": "integration-console",
            "base_url": state.get("base_url"),
            "system_code": state.get("system_code"),
            "logged_in": bool(current_login_name(state)),
            "login_name": current_login_name(state),
        })

    @app.route("/session")
    def session_debug():
        state = get_state()
        payload = public_state(state)
        payload["logged_in"] = bool(current_login_name(state))
        return jsonify(payload)

    @app.errorhandler(404)
    def not_found(_):
        return render_template("error.html", code=404,
                               message="页面不存在"), 404

    @app.errorhandler(500)
    def server_error(exc):
        return render_template("error.html", code=500,
                               message="服务内部错误：%s" % exc), 500
