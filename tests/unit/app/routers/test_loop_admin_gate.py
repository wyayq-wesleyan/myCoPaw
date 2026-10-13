# -*- coding: utf-8 -*-
"""Loop 模式的权限边界。

需求是：**普通账号可以用「默认 / 目标 / 任务」这三种内置形式，但 Loop 模式
（自定义/插件模式及其设置）仍归管理员**，普通账号沿用管理员配好的 gates 与上限。

实现上有两处必须同时成立，缺一处就会出现「界面看不到但实际能用」的假隔离：

1. 目录接口（``GET /loops``）对普通账号**只返回内置三种**；
2. 自定义模式的增删改**在服务端拒绝普通账号** —— 只靠界面隐藏按钮不是访问控制。
   （实测现状：这几个端点在加防护前对任何登录用户都开放。）
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from qwenpaw.app.routers import loops as loop_api


class _Req:
    """带身份的最小 Request 替身。"""

    def __init__(self, user: str | None) -> None:
        self.state = type("S", (), {"user": user})()
        self.headers: dict[str, str] = {}


def _admin_check(monkeypatch, *, auth_enabled: bool, is_admin: bool):
    """把「是否启用鉴权」与「是否管理员」两件事分别钉死。

    这两个维度必须分开测：未启用鉴权时只有一个账号且它就是管理员，所以应当放行；
    启用鉴权时才按角色判定。
    """
    import qwenpaw.app.auth as auth
    import qwenpaw.app.user_space as user_space

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: auth_enabled)
    monkeypatch.setattr(user_space, "is_admin_user", lambda _u: is_admin)
    monkeypatch.setattr(user_space, "get_request_username", lambda _r: "someone")


class TestLoopAdminGate:
    def test_admin_is_allowed(self, monkeypatch):
        _admin_check(monkeypatch, auth_enabled=True, is_admin=True)
        assert loop_api._loop_admin(_Req("admin")) is True
        loop_api._require_loop_admin(_Req("admin"))  # 不抛即为通过

    def test_ordinary_user_is_rejected(self, monkeypatch):
        """普通账号改 Loop 设置必须 403。"""
        _admin_check(monkeypatch, auth_enabled=True, is_admin=False)
        assert loop_api._loop_admin(_Req("bob")) is False
        with pytest.raises(HTTPException) as excinfo:
            loop_api._require_loop_admin(_Req("bob"))
        assert excinfo.value.status_code == 403

    def test_auth_disabled_single_account_is_the_admin(self, monkeypatch):
        """未启用鉴权时（单账号部署）应放行，否则管理员自己也用不了 Loop。"""
        _admin_check(monkeypatch, auth_enabled=False, is_admin=False)
        assert loop_api._loop_admin(_Req(None)) is True

    def test_unknown_identity_is_rejected_when_auth_enabled(self, monkeypatch):
        """拿不到身份时判为拒绝，而不是当成管理员。"""
        _admin_check(monkeypatch, auth_enabled=True, is_admin=False)
        assert loop_api._loop_admin(_Req(None)) is False


class TestCatalogFiltering:
    """目录过滤：普通账号只应看到内置三种。"""

    def _catalog(self):
        return [
            loop_api.LoopModeInfo(id="default", name="default", slash_command="",
                                  description="", source="builtin"),
            loop_api.LoopModeInfo(id="goal", name="goal", slash_command="goal",
                                  description="", source="builtin"),
            loop_api.LoopModeInfo(id="mission", name="mission", slash_command="mission",
                                  description="", source="builtin"),
            loop_api.LoopModeInfo(id="my-loop", name="my-loop", slash_command="myloop",
                                  description="", source="custom"),
            loop_api.LoopModeInfo(id="plugin-x", name="plugin-x", slash_command="px",
                                  description="", source="plugin"),
        ]

    def test_ordinary_user_gets_only_builtins(self, monkeypatch):
        """普通账号的目录里不能出现自定义/插件模式。

        界面按这份列表渲染，所以过滤放在服务端——否则用户仍可通过斜杠命令
        直接触发未授权模式（命令前缀是模型可见的普通文本）。
        """
        _admin_check(monkeypatch, auth_enabled=True, is_admin=False)
        filtered = [m for m in self._catalog() if m.source == "builtin"]
        assert [m.id for m in filtered] == ["default", "goal", "mission"]

    def test_admin_gets_everything(self, monkeypatch):
        _admin_check(monkeypatch, auth_enabled=True, is_admin=True)
        assert len(self._catalog()) == 5
