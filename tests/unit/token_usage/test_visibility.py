"""Who may see whose usage — the single authorization rule.

Usage is published to every account as a performance measure, so this rule is
the thing that makes the numbers trustworthy: a normal account must never see
another account's figures, and must never get the combined total by accident.
"""

from __future__ import annotations

import pytest

from qwenpaw.token_usage import visibility


@pytest.fixture
def roles(monkeypatch):
    """Control `is_admin_user` without touching the auth store."""
    def _install(admins: set[str]):
        monkeypatch.setattr(
            "qwenpaw.app.user_space.is_admin_user",
            lambda username: username in admins,
        )

    return _install


def test_administrator_gets_the_global_view(roles):
    roles({"admin"})

    assert visibility.visible_account("admin") is None


def test_regular_account_is_scoped_to_itself(roles):
    roles({"admin"})

    assert visibility.visible_account("alice") == "alice"


def test_an_admin_named_other_than_admin_still_gets_the_global_view(roles):
    """Role membership decides, not the literal name "admin"."""
    roles({"admin", "张伟"})

    assert visibility.visible_account("张伟") is None


@pytest.mark.parametrize("username", ["", None])
def test_unidentified_caller_keeps_the_unrestricted_view(roles, username):
    """Auth-disabled deployments have no account to scope by."""
    roles({"admin"})

    assert visibility.visible_account(username) is None


def test_lookup_failure_never_widens_visibility(monkeypatch):
    """A broken role lookup must fail closed to the caller's own account."""
    def _boom(_username):
        raise RuntimeError("auth store unavailable")

    monkeypatch.setattr("qwenpaw.app.user_space.is_admin_user", _boom)

    assert visibility.visible_account("alice") == "alice"
