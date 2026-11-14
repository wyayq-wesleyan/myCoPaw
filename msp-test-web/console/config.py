"""联调测试台配置。

所有配置都来自环境变量，页面上的“连接设置”只覆盖当前会话。
"""

import os


def env_bool(name, default="false"):
    value = os.getenv(name, default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


APP_TITLE = os.getenv("MSP_TEST_APP_TITLE", "联调测试台")
DEFAULT_BASE_URL = os.getenv("MIDDLEWARE_BASE_URL", "http://127.0.0.1:9089").rstrip("/")
DEFAULT_SYSTEM_CODE = os.getenv("MSP_TEST_SYSTEM_CODE", "middleware").strip() or "middleware"
DEFAULT_TCMS_SYSTEM_CODE = (
    os.getenv("MSP_TEST_TCMS_SYSTEM_CODE", "").strip() or DEFAULT_SYSTEM_CODE
)
DEFAULT_ELC_SYSTEM_CODE = (
    os.getenv("MSP_TEST_ELC_SYSTEM_CODE", "").strip() or DEFAULT_SYSTEM_CODE
)
DEFAULT_SSO_LOGOUT_URL = os.getenv("MSP_TEST_SSO_LOGOUT_URL", "").strip()
DEFAULT_VERIFY_TLS = env_bool("MSP_TEST_VERIFY_TLS", "false")
DEFAULT_TIMEOUT = float(os.getenv("MSP_TEST_TIMEOUT", "20"))
SECRET_KEY = os.getenv("MSP_TEST_SECRET_KEY", "integration-console-internal-only-change-me")
STATE_TTL_SECONDS = int(os.getenv("MSP_TEST_STATE_TTL", str(8 * 3600)))
ALLOWED_RAW_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}

# 模块注册表：决定左侧导航和首页模块卡。
# 后续新增模块（例如 Matrix）只需要在这里加一项并实现对应 blueprint。
MODULES = [
    {
        "key": "auth",
        "name": "统一认证",
        "short": "AUTH",
        "desc": "SSO 登录跳转、token 校验、平台会话、登出",
        "icon": "shield",
        "route": "auth.login",
        "color": "blue",
        "items": ["登录跳转", "Token 会话", "用户/角色/组织", "登出"],
    },
    {
        "key": "msp",
        "name": "MSP 平台",
        "short": "MSP",
        "desc": "用户、组织、角色、权限、菜单、系统、消息",
        "icon": "users",
        "route": "msp.user",
        "color": "indigo",
        "items": ["用户信息", "权限编码", "菜单树", "角色/组织", "系统列表"],
    },
    {
        "key": "tcms",
        "name": "TCMS 案管",
        "short": "TCMS",
        "desc": "案件查询、档案、目标对象、流程发起与状态",
        "icon": "folder",
        "route": "tcms.cases",
        "color": "teal",
        "items": ["用户案件", "案件详情", "档案列表", "目标对象", "流程"],
    },
    {
        "key": "elc",
        "name": "ELC 审计",
        "short": "ELC",
        "desc": "执法日志写入：单条、登录、操作、批量",
        "icon": "file",
        "route": "elc.log",
        "color": "amber",
        "items": ["单条日志", "登录日志", "操作日志", "批量写入"],
    },
]

MODULE_MAP = {item["key"]: item for item in MODULES}
