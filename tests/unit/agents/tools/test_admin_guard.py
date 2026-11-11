# -*- coding: utf-8 -*-
"""Admin-only tool gate tests.

Roles are only ``admin`` and ``user``; a non-admin must be denied in the
backend even if the console/agent configuration exposes the tool.
"""

from __future__ import annotations

import pytest

from qwenpaw.agents.tools.admin_guard import admin_denial


def test_admin_is_allowed(monkeypatch):
    monkeypatch.setattr(
        "qwenpaw.agents.tools.user_files.resolve_account_username",
        lambda: "admin",
    )
    monkeypatch.setattr(
        "qwenpaw.app.user_space.is_admin_user",
        lambda username: username == "admin",
    )
    assert admin_denial("测试工具") is None


def test_regular_user_is_denied(monkeypatch):
    monkeypatch.setattr(
        "qwenpaw.agents.tools.user_files.resolve_account_username",
        lambda: "alice",
    )
    monkeypatch.setattr(
        "qwenpaw.app.user_space.is_admin_user",
        lambda username: False,
    )
    message = admin_denial("浏览器自动化")
    assert message is not None
    assert "仅管理员可用" in message
    assert "浏览器自动化" in message


def test_identity_failure_denies(monkeypatch):
    from qwenpaw.agents.tools.user_files import AccountScopeError

    def _raise():
        raise AccountScopeError("无法确定账号")

    monkeypatch.setattr(
        "qwenpaw.agents.tools.user_files.resolve_account_username",
        _raise,
    )
    message = admin_denial("桌面截图")
    assert message is not None
    assert "拒绝" in message


def test_admin_check_failure_denies(monkeypatch):
    monkeypatch.setattr(
        "qwenpaw.agents.tools.user_files.resolve_account_username",
        lambda: "alice",
    )

    def _raise(_username):
        raise RuntimeError("auth store down")

    monkeypatch.setattr(
        "qwenpaw.app.user_space.is_admin_user",
        _raise,
    )
    message = admin_denial("外部智能体代理")
    assert message is not None
    assert "安全" in message
