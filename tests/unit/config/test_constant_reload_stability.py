# -*- coding: utf-8 -*-
"""``EnvVarLoader`` 必须在 ``constant`` 被 reload 后仍是同一个类对象。

为什么这件事重要：11 个生产模块用 ``from ..constant import EnvVarLoader`` 按值导入，
测试也靠 ``patch("qwenpaw.constant.EnvVarLoader.get_str")`` 控制环境。``importlib.reload``
会在同一命名空间里重新执行模块并产生一个**新的类对象**，于是：

1. 已按值导入的模块继续持有旧类，之后对 ``constant.EnvVarLoader`` 的 patch 静默失效
   —— 实测表现为「先跑常量重载测试、再跑 tool_guard 测试，9 个用例失败」；
2. 生产代码里若有 reload（插件热加载、配置重载），同一个进程里就会同时存在两个
   ``EnvVarLoader``，「修改了环境变量却读不到」会以偶发形式出现。

修复方式是 reload 时保留原类对象，同时仍然重算模块级常量（这才是 reload 的目的）。
"""
from __future__ import annotations

import importlib


def test_reload_preserves_env_var_loader_identity() -> None:
    import qwenpaw.constant as constant

    before = constant.EnvVarLoader
    reloaded = importlib.reload(constant)

    assert reloaded is constant, "reload should return the same module object"
    assert reloaded.EnvVarLoader is before, (
        "reload created a new EnvVarLoader class; modules that imported it by "
        "value now hold a stale class and will ignore patches/env changes"
    )


def test_by_value_imports_keep_working_after_reload(monkeypatch) -> None:
    """按值导入的模块必须还能看到 patch 后的行为。"""
    import qwenpaw.constant as constant
    from qwenpaw.security.tool_guard import utils as guard_utils

    assert guard_utils.EnvVarLoader is constant.EnvVarLoader

    calls: list[str] = []

    def _fake_get_str(name: str, *args, **kwargs):
        calls.append(name)
        return "execute_shell_command"

    monkeypatch.setattr(constant.EnvVarLoader, "get_str", staticmethod(_fake_get_str))
    importlib.reload(constant)
    monkeypatch.setattr(constant.EnvVarLoader, "get_str", staticmethod(_fake_get_str))

    try:
        result = guard_utils.resolve_guarded_tools()
        assert result == {"execute_shell_command"}, (
            f"by-value import did not see the patched loader: {result!r}"
        )
        assert calls, "the patched get_str was never called"
    finally:
        # Leave the module consistent for the rest of the session.
        importlib.reload(constant)


def test_reload_still_recomputes_module_constants(monkeypatch) -> None:
    """保留类对象不能妨碍 reload 重算常量的本职功能。"""
    import qwenpaw.constant as constant

    monkeypatch.setenv("QWENPAW_LLM_STREAM_FIRST_CONTENT_TIMEOUT", "45")
    reloaded = importlib.reload(constant)
    assert reloaded.LLM_STREAM_FIRST_CONTENT_TIMEOUT == 45.0

    monkeypatch.delenv("QWENPAW_LLM_STREAM_FIRST_CONTENT_TIMEOUT", raising=False)
    importlib.reload(constant)
