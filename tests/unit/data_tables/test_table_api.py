"""The table-definition core is shared by the console API and the agent tool.

The agent path deliberately allows an empty column list: the chat user often
cannot describe their columns, and the importer infers real names and types
from the file header when the table is created on first import.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

# psycopg2 ships in the base deploy image (deploy/Dockerfile.base), not in the
# Python project metadata, so these tests only run where it is installed.
pytest.importorskip("psycopg2")

from qwenpaw.data_tables import table_api  # noqa: E402


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append((" ".join(str(sql).split()), params))

    def fetchone(self):
        return self._conn.results.pop(0) if self._conn.results else None


class _FakeConn:
    def __init__(self, results):
        self.results = list(results)
        self.executed: list[tuple[str, object]] = []
        self.committed = False

    def cursor(self, *args, **kwargs):
        return _FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


@pytest.fixture
def table_api_env(monkeypatch):
    """Wire the table core to a fake connection with scripted results."""
    conn = _FakeConn(results=[])

    monkeypatch.setattr(table_api, "_get_conn", lambda: conn)
    monkeypatch.setattr(table_api, "_put_conn", lambda _conn: None)
    monkeypatch.setattr(table_api, "ensure_user_database", lambda username: f"db_{username}")
    monkeypatch.setattr(table_api, "generate_table_name", lambda _a, _b: "tb_hzxzs_test_0001")
    return conn


def _insert_params(conn: _FakeConn) -> tuple:
    for sql, params in conn.executed:
        if sql.startswith("INSERT INTO"):
            return params
    raise AssertionError("no INSERT was executed")


def test_create_table_record_accepts_no_columns(table_api_env):
    """The agent path creates a table before columns are known."""
    table_api_env.results = [None, (7,)]

    record = table_api.create_table_record("u1", description="2024 年销售明细")

    assert record == {"id": "7", "table_name": "tb_hzxzs_test_0001"}
    assert _insert_params(table_api_env)[-1] == json.dumps([])
    assert table_api_env.committed is True


def test_create_table_record_trims_and_drops_blank_column_notes(table_api_env):
    table_api_env.results = [None, (8,)]

    table_api.create_table_record(
        "u1",
        display_name="销售",
        description="销售明细",
        columns=["  订单号  ", "   ", "金额"],
    )

    notes = json.loads(_insert_params(table_api_env)[-1])
    assert [note["comment"] for note in notes] == ["订单号", "金额"]
    assert all(note["column_name"] is None for note in notes)


def test_create_table_record_scopes_the_database_to_the_caller(table_api_env):
    table_api_env.results = [None, (9,)]

    table_api.create_table_record("alice", description="alice 的表")

    assert _insert_params(table_api_env)[0] == "alice"
    assert _insert_params(table_api_env)[1] == "db_alice"


def test_create_table_record_rejects_duplicate_name(table_api_env):
    table_api_env.results = [(1,)]

    with pytest.raises(HTTPException) as excinfo:
        table_api.create_table_record("u1", description="重复")

    assert excinfo.value.status_code == 409


def test_find_owned_table_never_falls_back_to_an_admin_override(table_api_env):
    """A chat tool must not reach another account's table."""
    table_api_env.results = [None]

    table_api.find_owned_table("alice", "tb_hzxzs_test_0001")

    sql, params = table_api_env.executed[-1]
    assert "owner_username = %s" in sql
    assert "is_admin" not in sql
    assert params[0] == "alice"
    assert "tb_hzxzs_test_0001" in params


def test_find_owned_table_returns_none_without_a_reference(table_api_env):
    assert table_api.find_owned_table("alice", "  ") is None
    assert table_api_env.executed == []
