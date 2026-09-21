"""A write tool may only ever read files inside the calling account's workspace.

Approval is hard-disabled in this deployment, so these containment checks are
the actual gate for "import this file" / "upload this file" tool calls.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from qwenpaw.agents.tools import user_files

EXTS = frozenset({".csv", ".xlsx"})
CSV = "name,amount\n甲,1\n"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Point the resolver at a temporary workspace with a chat media dir."""
    monkeypatch.delenv("CONSOLE_MEDIA_DIR", raising=False)
    monkeypatch.setattr(user_files, "get_current_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(user_files, "resolve_account_username", lambda: "u1")
    media = tmp_path / "media"
    media.mkdir()
    return tmp_path, media


def _write(path: Path, text: str = CSV) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_bare_filename_matches_uuid_prefixed_chat_upload(workspace):
    _, media = workspace
    uploaded = _write(media / "e5f1a2_销售数据.xlsx")

    resolved, error = user_files.resolve_user_file("销售数据.xlsx", EXTS)

    assert error == ""
    assert resolved == uploaded.resolve()


def test_newest_matching_upload_wins(workspace):
    _, media = workspace
    older = _write(media / "aaa_订单.csv")
    newer = _write(media / "bbb_订单.csv")
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    resolved, error = user_files.resolve_user_file("订单.csv", EXTS)

    assert error == ""
    assert resolved == newer.resolve()


def test_absolute_path_inside_workspace_is_allowed(workspace):
    root, _ = workspace
    inside = _write(root / "reports" / "ok.csv")

    resolved, error = user_files.resolve_user_file(str(inside), EXTS)

    assert error == ""
    assert resolved == inside.resolve()


def test_absolute_path_outside_workspace_is_rejected(workspace, tmp_path):
    outside = _write(tmp_path.parent / "someone-else" / "secret.csv")

    resolved, error = user_files.resolve_user_file(str(outside), EXTS)

    assert resolved is None
    assert "不在你的工作区内" in error


def test_symlink_pointing_outside_workspace_is_rejected(workspace, tmp_path):
    """resolve() dereferences the link, so the escape fails containment."""
    root, _ = workspace
    outside = _write(tmp_path.parent / "another-account" / "leak.csv")
    link = root / "innocent.csv"
    link.symlink_to(outside)

    resolved, error = user_files.resolve_user_file(str(link), EXTS)

    assert resolved is None
    assert "不在你的工作区内" in error


def test_traversal_reference_cannot_escape(workspace, tmp_path):
    _write(tmp_path.parent / "up-and-out.csv")

    resolved, error = user_files.resolve_user_file("../../up-and-out.csv", EXTS)

    assert resolved is None
    assert error


def test_relative_subdirectory_reference_is_allowed(workspace):
    root, _ = workspace
    target = _write(root / "reports" / "2024.csv")

    resolved, error = user_files.resolve_user_file("reports/2024.csv", EXTS)

    assert error == ""
    assert resolved == target.resolve()


def test_unsupported_extension_is_rejected(workspace):
    _, media = workspace
    _write(media / "notes.txt", "hello")

    resolved, error = user_files.resolve_user_file("notes.txt", EXTS)

    assert resolved is None
    assert "不支持的文件格式" in error


def test_missing_file_reports_what_is_available(workspace):
    _, media = workspace
    _write(media / "aaa_真实数据.csv")

    resolved, error = user_files.resolve_user_file("不存在.csv", EXTS)

    assert resolved is None
    assert "没有找到" in error
    assert "aaa_真实数据.csv" in error


def test_empty_reference_is_rejected(workspace):
    resolved, error = user_files.resolve_user_file("   ", EXTS)

    assert resolved is None
    assert "请提供" in error


def test_directory_is_not_accepted_as_a_file(workspace):
    root, _ = workspace
    (root / "folder.csv").mkdir()

    resolved, error = user_files.resolve_user_file(str(root / "folder.csv"), EXTS)

    assert resolved is None
    assert error


# ---------------------------------------------------------------------------
# Account resolution must fail closed
#
# The tools used to fall back to ``"admin"`` whenever the account could not be
# determined. With authentication enabled that is fail-open: any request whose
# identity failed to resolve -- a missing request hook, an unexpected context
# value -- silently became the administrator and reached the admin workspace.
# ---------------------------------------------------------------------------


@pytest.fixture
def no_identity(monkeypatch):
    """Clear the context values the resolver reads."""
    from qwenpaw.app.agent_context import set_current_user_id
    from qwenpaw.app.agent_context import set_current_agent_id

    set_current_user_id("")
    set_current_agent_id("")
    monkeypatch.setattr(
        user_files,
        "get_current_user_id",
        lambda: "",
    )
    monkeypatch.setattr(
        user_files,
        "get_current_agent_id",
        lambda: "",
    )


def test_unresolved_account_is_refused_when_auth_is_enabled(
    monkeypatch,
    no_identity,
):
    monkeypatch.setenv("QWENPAW_AUTH_ENABLED", "true")

    with pytest.raises(user_files.AccountScopeError):
        user_files.resolve_account_username()


def test_unresolved_account_falls_back_to_admin_when_auth_is_off(
    monkeypatch,
    no_identity,
):
    """A single-account deployment has only ``admin``; that is not a guess."""
    monkeypatch.setenv("QWENPAW_AUTH_ENABLED", "false")

    assert user_files.resolve_account_username() == "admin"


def test_a_known_account_still_resolves(monkeypatch):
    monkeypatch.setenv("QWENPAW_AUTH_ENABLED", "true")
    monkeypatch.setattr(user_files, "get_current_user_id", lambda: "bob")

    assert user_files.resolve_account_username() == "bob"


def test_the_legacy_default_value_is_not_an_account(monkeypatch):
    """``default`` is a UI placeholder, not an account, so it must not pass."""
    monkeypatch.setenv("QWENPAW_AUTH_ENABLED", "true")
    monkeypatch.setattr(user_files, "get_current_user_id", lambda: "default")
    monkeypatch.setattr(user_files, "get_current_agent_id", lambda: "")

    with pytest.raises(user_files.AccountScopeError):
        user_files.resolve_account_username()


def test_the_default_agent_placeholder_does_not_resolve_to_its_owner(
    monkeypatch,
):
    """The literal ``default`` agent id must not be treated as a real agent.

    The request hook writes ``ctx.agent_id or "default"``, and the deployment
    contains an agent literally named ``default`` owned by the administrator.
    Resolving that placeholder would turn "identity unknown" into "act as
    admin" -- a fail-open that survived an earlier fix which only checked the
    fallback path. Verified end to end, this was a real hole.
    """
    from types import SimpleNamespace

    import qwenpaw.config as config_module

    monkeypatch.setenv("QWENPAW_AUTH_ENABLED", "true")
    monkeypatch.setattr(user_files, "get_current_user_id", lambda: "")
    monkeypatch.setattr(user_files, "get_current_agent_id", lambda: "default")

    fake = SimpleNamespace(
        agents=SimpleNamespace(
            profiles={
                "default": SimpleNamespace(owner_username="admin"),
            },
        ),
    )
    monkeypatch.setattr(config_module, "load_config", lambda: fake)

    with pytest.raises(user_files.AccountScopeError):
        user_files.resolve_account_username()


def test_a_real_agent_id_still_resolves_its_owner(monkeypatch):
    """The placeholder guard must not break genuine agent-based resolution."""
    from types import SimpleNamespace

    import qwenpaw.config as config_module

    monkeypatch.setenv("QWENPAW_AUTH_ENABLED", "true")
    monkeypatch.setattr(user_files, "get_current_user_id", lambda: "")
    monkeypatch.setattr(user_files, "get_current_agent_id", lambda: "bob-bot")

    fake = SimpleNamespace(
        agents=SimpleNamespace(
            profiles={
                "bob-bot": SimpleNamespace(owner_username="bob"),
            },
        ),
    )
    monkeypatch.setattr(config_module, "load_config", lambda: fake)

    assert user_files.resolve_account_username() == "bob"
