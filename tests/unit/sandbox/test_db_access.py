# -*- coding: utf-8 -*-
"""Unit tests for per-account DB role/proxy helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from qwenpaw.sandbox import db_access


def test_role_name_is_stable_and_distinct():
    assert db_access._role_name("alice") == db_access._role_name("alice")
    assert db_access._role_name("alice") != db_access._role_name("bob")
    assert db_access._role_name("alice").startswith("sbx_")
    assert len(db_access._role_name("alice")) <= 63


def test_reserved_database_is_refused(monkeypatch):
    monkeypatch.setattr(
        "qwenpaw.data_tables.connection._database_name_for_username",
        lambda _username: "huizhi_knowledge",
    )
    with pytest.raises(db_access.DbAccessError):
        db_access._account_database("someone")


def test_pg_environment_uses_socket_and_role(tmp_path):
    access = db_access.AccountDbAccess(
        username="alice",
        database="alice",
        role="sbx_abc",
        password="secret",
        socket_dir=tmp_path / "pgsock",
        socket_path=tmp_path / "pgsock" / ".s.PGSQL.5432",
    )
    env = db_access.pg_environment(access)
    assert env["PGHOST"] == str(tmp_path / "pgsock")
    assert env["PGPORT"] == "5432"
    assert env["PGUSER"] == "sbx_abc"
    assert env["PGDATABASE"] == "alice"
    assert env["PGPASSWORD"] == "secret"
    assert env["PGSSLMODE"] == "disable"
    assert "QWENPAW_DATA_TABLES_DB_PASSWORD" not in env


def test_socket_name_is_short_enough():
    # Real production path: /app/working/workspaces/u_<24 hex>/outputs/pgsock/
    path = f"/app/working/workspaces/u_{'a' * 24}/outputs/pgsock/{db_access._SOCKET_NAME}"
    # AF_UNIX paths are limited to ~107 bytes on Linux.
    assert len(path) < 107
