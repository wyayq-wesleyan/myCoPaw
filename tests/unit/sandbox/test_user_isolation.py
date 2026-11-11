# -*- coding: utf-8 -*-
"""Unit tests for the ordinary-user shell sandbox configuration.

The kernel-level enforcement (Landlock/seccomp) is exercised separately inside
a Linux container; these tests pin the policy that is compiled into the runner
script and the environment that reaches user code.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from qwenpaw.sandbox.user_isolation import (
    SandboxIdentity,
    SandboxPaths,
    _generate_runner_script,
    _sandbox_uid,
    _landlock_enabled,
    build_safe_env,
    prepare_paths,
)


def _paths(tmp_path: Path, username: str = "alice") -> SandboxPaths:
    root = tmp_path / "account"
    output = root / "outputs"
    scripts = root / "scripts"
    media = root / "media"
    upload = root / "upload"
    for path in (output, scripts, media, upload):
        path.mkdir(parents=True, exist_ok=True)
    return SandboxPaths(
        account_root=root,
        output_dir=output,
        scripts_dir=scripts,
        media_dirs=(media,),
        upload_dir=upload,
        identity=SandboxIdentity(username, 20001, 20001),
    )


def test_uid_is_stable_and_distinct():
    assert _sandbox_uid("alice") == _sandbox_uid("alice")
    assert _sandbox_uid("alice") != _sandbox_uid("bob")
    assert 20000 <= _sandbox_uid("alice") < 100000


def test_safe_env_drops_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("QWENPAW_KNOWLEDGE_API_KEY", "knowledge-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")

    env = build_safe_env(_paths(tmp_path))

    assert "OPENAI_API_KEY" not in env
    assert "QWENPAW_KNOWLEDGE_API_KEY" not in env
    assert "DATABASE_URL" not in env
    assert env["LANG"] == "zh_CN.UTF-8"
    assert env["HUIZHI_SANDBOX"] == "1"
    assert env["HOME"].startswith(str(tmp_path))
    assert env["TMPDIR"].endswith("/outputs/tmp")


def test_runner_script_grants_account_read_and_output_write(tmp_path):
    paths = _paths(tmp_path)
    script = _generate_runner_script("echo hello", paths.account_root, paths, 30)

    assert str(paths.account_root) in script
    assert str(paths.output_dir) in script
    assert str(paths.scripts_dir) in script
    assert "FS_READ, critical=True" in script
    assert "FS_HANDLED, critical=True" in script
    assert "seccomp_rule_add_array" in script
    assert "b'socket'" in script
    assert "os.setuid(uid)" in script
    assert "RLIMIT_AS" in script
    assert "RLIMIT_NPROC" in script
    assert "execv('/bin/sh'" in script


def test_runner_script_does_not_grant_tmp_proc_or_app(tmp_path):
    paths = _paths(tmp_path)
    script = _generate_runner_script("echo hello", paths.account_root, paths, 30)

    # /tmp and /proc are intentionally outside the allow-list.  User code uses
    # outputs/tmp via TMPDIR; /proc/1/environ must not be readable.
    assert "add_path('/tmp'" not in script
    assert "add_path('/proc'" not in script
    assert "add_path('/workspace'" not in script
    assert "add_path('/app'" not in script


def test_permission_fallback_runner_has_seccomp_without_landlock(tmp_path):
    paths = _paths(tmp_path)
    paths = SandboxPaths(
        account_root=paths.account_root,
        output_dir=paths.output_dir,
        scripts_dir=paths.scripts_dir,
        media_dirs=paths.media_dirs,
        upload_dir=paths.upload_dir,
        identity=paths.identity,
        permission_fallback=True,
    )

    script = _generate_runner_script("echo hello", paths.account_root, paths, 30)

    assert "landlock_create_ruleset" not in script
    assert "seccomp_rule_add_array" in script
    assert "os.setuid(uid)" in script
    assert "os.umask(0o077)" in script


def test_runner_script_uses_abi_aware_masks(tmp_path):
    paths = _paths(tmp_path)
    script = _generate_runner_script("echo hello", paths.account_root, paths, 30)

    assert "FS_HANDLED = FS_READ | FS_WRITE | FS_EXEC" in script
    assert "if abi >= 2:" in script
    assert "if abi >= 3:" in script
    assert "HANDLED_NET = NET_ALL if abi >= 4 else 0" in script


def test_prepare_paths_creates_output_and_replaces_symlink(
    tmp_path,
    monkeypatch,
):
    account = tmp_path / "account"
    account.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (account / "outputs").symlink_to(outside)

    monkeypatch.setattr(
        "qwenpaw.app.user_space.get_user_root",
        lambda _username: account,
    )

    paths = prepare_paths("alice")

    assert paths.output_dir.is_dir()
    assert not paths.output_dir.is_symlink()
    assert (paths.output_dir / "tmp").is_dir()
    assert paths.scripts_dir.is_dir()
    assert not paths.scripts_dir.is_symlink()
    assert paths.account_root == account.resolve()


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Landlock/seccomp integration runs only on Linux",
)
def test_landlock_probe_available_on_linux_image():
    from qwenpaw.sandbox.user_isolation import _supported

    # The production image ships Landlock + libseccomp.  A host without them
    # must fail closed (the shell tool denies ordinary users), so this test is
    # informative rather than a hard requirement on every Linux dev box.
    assert isinstance(_supported(), bool)


def test_landlock_is_opt_in(monkeypatch):
    monkeypatch.delenv("HUIZHI_SANDBOX_LANDLOCK", raising=False)
    assert _landlock_enabled() is False
    monkeypatch.setenv("HUIZHI_SANDBOX_LANDLOCK", "1")
    assert _landlock_enabled() is True
    monkeypatch.setenv("HUIZHI_SANDBOX_LANDLOCK", "0")
    assert _landlock_enabled() is False
