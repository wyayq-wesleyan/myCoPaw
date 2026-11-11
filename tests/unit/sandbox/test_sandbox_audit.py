# -*- coding: utf-8 -*-
"""Structured sandbox audit records."""

from __future__ import annotations

import hashlib
import json
import os

from qwenpaw.sandbox.audit import record_sandbox_execution
from qwenpaw.sandbox.config import ExecutionResult


def test_record_writes_root_only_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("QWENPAW_LOG_DIR", str(tmp_path))
    command = "echo secret-in-command"

    record_sandbox_execution(
        "alice",
        20001,
        str(tmp_path),
        command,
        ExecutionResult(
            exit_code=0,
            stdout="ok\n",
            stderr="",
            duration_ms=12,
        ),
    )

    path = tmp_path / "sandbox-audit.jsonl"
    assert path.is_file()
    assert (path.stat().st_mode & 0o777) == 0o600
    entry = json.loads(path.read_text("utf-8").splitlines()[0])
    assert entry["kind"] == "user_sandbox_exec"
    assert entry["user"] == "alice"
    assert entry["uid"] == 20001
    assert entry["command"] == command
    assert entry["command_sha256"] == hashlib.sha256(
        command.encode("utf-8"),
    ).hexdigest()
    assert entry["exit_code"] == 0
    assert entry["duration_ms"] == 12
    assert entry["violation"] == ""
    assert entry["stdout_bytes"] == 3


def test_record_truncates_command_but_hashes_all(tmp_path, monkeypatch):
    monkeypatch.setenv("QWENPAW_LOG_DIR", str(tmp_path))
    command = "x" * 20000

    record_sandbox_execution(
        "bob",
        20002,
        str(tmp_path),
        command,
        ExecutionResult(exit_code=1, stdout="", stderr="denied"),
    )

    entry = json.loads(
        (tmp_path / "sandbox-audit.jsonl").read_text("utf-8").splitlines()[0],
    )
    assert len(entry["command"]) < len(command)
    assert "[truncated " in entry["command"]
    assert entry["command_sha256"] == hashlib.sha256(
        command.encode("utf-8"),
    ).hexdigest()
    assert entry["exit_code"] == 1


def test_record_failure_does_not_raise(monkeypatch, tmp_path):
    # Point the audit at a path whose parent is a regular file; record() must
    # swallow the error so the command result is unaffected.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("QWENPAW_LOG_DIR", str(blocker / "logs"))

    record_sandbox_execution(
        "alice",
        20001,
        str(tmp_path),
        "echo hi",
        ExecutionResult(exit_code=0, stdout="hi", stderr=""),
    )
