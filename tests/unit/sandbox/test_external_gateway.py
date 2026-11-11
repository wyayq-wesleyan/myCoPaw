# -*- coding: utf-8 -*-
"""Unit tests for the external data/API gateway policy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qwenpaw.sandbox import external_gateway as gw


def test_sql_guard_accepts_select_and_with():
    assert gw._query_sql_guard("SELECT 1") == "SELECT 1"
    assert gw._query_sql_guard("WITH x AS (SELECT 1) SELECT * FROM x").startswith(
        "WITH",
    )


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a=1",
        "DELETE FROM t",
        "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
        "SELECT 1; DROP TABLE t",
        "SELECT * INTO OUTFILE '/tmp/x' FROM t",
        "SELECT dblink('x','SELECT 1')",
        "SELECT pg_read_file('/etc/passwd')",
        "COPY t TO '/tmp/x'",
    ],
)
def test_sql_guard_rejects_writes_and_sensitive_functions(sql):
    with pytest.raises(gw.ExternalGatewayError):
        gw._query_sql_guard(sql)


def test_sql_guard_does_not_treat_string_literal_as_comment():
    sql = "SELECT '-- not a comment' AS note"
    assert gw._query_sql_guard(sql) == sql


def test_host_allowlist_exact_and_wildcard():
    allow = ["api.example.com", "*.example.org"]
    assert gw._host_allowed("api.example.com", allow)
    assert gw._host_allowed("a.example.org", allow)
    assert gw._host_allowed("example.org", allow) is False
    assert gw._host_allowed("evil.com", allow) is False


def test_safe_json_handles_decimal_and_bytes():
    import base64
    import decimal

    assert gw._safe_json(decimal.Decimal("1.20")) == "1.20"
    assert gw._safe_json(b"abc") == base64.b64encode(b"abc").decode("ascii")


def test_public_sources_respects_user_allowlist(tmp_path, monkeypatch):
    cfg = {
        "sources": {
            "open": {"type": "postgres", "allow_all": True},
            "alice_only": {"type": "mysql", "allowed_users": ["alice"]},
            "bob_only": {"type": "oracle", "allowed_users": ["bob"]},
            "nobody": {"type": "hive"},
        },
        "http_allow": [],
    }
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(cfg), "utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(gw, "_secret_config_path", lambda: path)

    names = {item["name"] for item in gw._public_sources("alice")}
    assert names == {"open", "alice_only"}


def test_source_for_denies_unlisted_user(tmp_path, monkeypatch):
    cfg = {
        "sources": {
            "bob_only": {"type": "postgres", "allowed_users": ["bob"]},
        },
        "http_allow": [],
    }
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(cfg), "utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(gw, "_secret_config_path", lambda: path)

    with pytest.raises(gw.ExternalGatewayError):
        gw._source_for("alice", "bob_only")
    assert gw._source_for("bob", "bob_only")["type"] == "postgres"


def test_config_with_group_readable_mode_is_refused(tmp_path, monkeypatch):
    cfg = {"sources": {"x": {"type": "postgres", "allow_all": True}}, "http_allow": []}
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(cfg), "utf-8")
    path.chmod(0o644)
    monkeypatch.setattr(gw, "_secret_config_path", lambda: path)

    loaded = gw._load_config()
    assert loaded["sources"] == {}
    assert loaded["http_allow"] == []
