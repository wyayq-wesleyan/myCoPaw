# -*- coding: utf-8 -*-
"""Preview metadata must be matched to columns by NAME, not by position.

``SELECT *`` returns the physical columns of the real table, which start with the
``id`` primary key. The registry only describes the user-declared columns and has
no ``id`` entry, so the two lists differ in length by one. The frontend used to
pair them by index, which shifted every header label by one — the ``id`` column
displayed the first user description, each later label showed its neighbour's,
and the final column lost its label.

The backend now returns ``column_meta`` aligned to the physical order, resolved
by name. These tests pin that resolution, including the case the old positional
logic could never handle: a description whose text differs from the physical
column name.
"""
from __future__ import annotations

import psycopg2.extras
import pytest

from qwenpaw.data_tables import preview_api


class _Cur:
    def __init__(self, table_row, col_names, rows):
        self._table = table_row
        self._col_names = col_names
        self._rows = rows
        self.description = [(n,) for n in col_names] if col_names else None
        self._mode = "table"

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, *_args, **_kwargs):
        self._mode = "data" if "SELECT * FROM" in sql and "registry" not in sql else "table"
        return None

    def fetchone(self):
        return self._table

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, table_row, col_names, rows):
        self._cur = _Cur(table_row, col_names, rows)

    def cursor(self, **_kwargs):
        return self._cur


def _patch(monkeypatch, table_row, col_names, rows):
    monkeypatch.setattr(
        preview_api, "_get_conn", lambda: _Conn(table_row, None, None),
    )
    monkeypatch.setattr(preview_api, "_put_conn", lambda _c: None)
    monkeypatch.setattr(
        preview_api,
        "get_target_connection",
        lambda _db, _schema=None: _Conn(table_row, col_names, rows),
    )
    monkeypatch.setattr(preview_api, "put_target_connection", lambda *_a: None)


def _captured_schema(monkeypatch, table_row, col_names, rows):
    """Patch the connection helper and record which schema preview asked for."""
    seen: list[str | None] = []

    def _conn(db, schema=None):
        seen.append(schema)
        return _Conn(table_row, col_names, rows)

    monkeypatch.setattr(
        preview_api, "_get_conn", lambda: _Conn(table_row, None, None),
    )
    monkeypatch.setattr(preview_api, "_put_conn", lambda _c: None)
    monkeypatch.setattr(preview_api, "get_target_connection", _conn)
    monkeypatch.setattr(preview_api, "put_target_connection", lambda *_a: None)
    return seen


def _table(columns, **over):
    base = {
        "owner_username": "admin",
        "initialized": True,
        "db_name": "db",
        "table_name": "tb_x",
        "row_count": 2,
        "columns": columns,
    }
    base.update(over)
    return base


def test_id_primary_key_does_not_borrow_a_description(monkeypatch) -> None:
    """The first header must not show the first user description."""
    columns = [
        {"comment": "备注信息", "column_name": "备注信息", "type": "TEXT"},
        {"comment": "用户号码", "column_name": "用户号码", "type": "TEXT"},
    ]
    _patch(
        monkeypatch,
        _table(columns),
        ["id", "备注信息", "用户号码"],
        [(1, "a", "138")],
    )

    result = preview_api._preview_table_sync("t", "admin", 100)

    meta = result["column_meta"]
    assert [m["name"] for m in meta] == ["id", "备注信息", "用户号码"]
    assert meta[0]["comment"] == "", "id borrowed a description"
    assert meta[0]["is_primary_key"] is True
    # Each description stays on its own column.
    assert meta[1]["comment"] == "备注信息"
    assert meta[2]["comment"] == "用户号码"


def test_descriptions_are_matched_by_name_not_position(monkeypatch) -> None:
    """Physical names differing from descriptions still resolve correctly.

    This is the case positional pairing can never get right: it would attach
    "第一列含义" to the physical column ``c1`` regardless of what ``c1`` is.
    """
    columns = [
        {"comment": "第一列含义", "column_name": "c1", "type": "TEXT"},
        {"comment": "第二列含义", "column_name": "c2", "type": "TEXT"},
    ]
    _patch(monkeypatch, _table(columns), ["id", "c1", "c2"], [(1, "v1", "v2")])

    meta = preview_api._preview_table_sync("t", "admin", 100)["column_meta"]

    assert [m["name"] for m in meta] == ["id", "c1", "c2"]
    assert meta[0]["comment"] == ""
    assert meta[1]["comment"] == "第一列含义"
    assert meta[2]["comment"] == "第二列含义"


def test_column_meta_length_always_matches_columns(monkeypatch) -> None:
    """meta and columns must stay indexable against each other."""
    columns = [
        {"comment": f"含义{i}", "column_name": f"col{i}", "type": "TEXT"}
        for i in range(10)
    ]
    names = ["id"] + [f"col{i}" for i in range(10)]
    _patch(monkeypatch, _table(columns), names, [tuple([1] + list(range(10)))])

    result = preview_api._preview_table_sync("t", "admin", 100)

    assert len(result["column_meta"]) == len(result["columns"]) == 11
    for name, meta in zip(result["columns"], result["column_meta"]):
        assert name == meta["name"]


def test_undeclared_column_has_no_comment(monkeypatch) -> None:
    """A physical column with no registry entry must not steal a neighbour's."""
    columns = [{"comment": "只有一列", "column_name": "known", "type": "TEXT"}]
    _patch(monkeypatch, _table(columns), ["id", "known", "extra"], [(1, "a", "b")])

    meta = preview_api._preview_table_sync("t", "admin", 100)["column_meta"]
    by_name = {m["name"]: m for m in meta}

    assert by_name["known"]["comment"] == "只有一列"
    assert by_name["extra"]["comment"] == ""
    assert by_name["id"]["comment"] == ""


def test_preview_returns_the_connection_under_the_same_schema(monkeypatch) -> None:
    """借出与归还必须带同一个 schema —— 否则连接会落到错误的池。

    池按 ``(db_name, schema)`` 分键。借出时按表所属路径的 schema、归还时按默认
    ``None``，会让限域池一直把这条连接记为已借出（两次查询后该 schema 上就开始
    报「连接池已满」），同时连接还可能被放进另一个池、把 ``search_path`` 带过去。

    这条测试刻意**不 stub** 归还调用：早期版本把 ``put_target_connection`` 替换成
    ``lambda *_a: None``，于是「参数少传一个」这种缺陷在测试里永远看不见。
    """
    columns = [{"comment": "c", "column_name": "c", "type": "TEXT"}]
    row = _table(columns)
    path_id = "11111111-2222-3333-4444-555555555555"
    schema = f"p_{path_id.replace('-', '')}"
    monkeypatch.setattr(preview_api, "schema_for_registry_row", lambda _row: schema)

    calls: list[tuple] = []
    monkeypatch.setattr(
        preview_api, "_get_conn", lambda: _Conn(row, None, None),
    )
    monkeypatch.setattr(preview_api, "_put_conn", lambda _c: None)
    monkeypatch.setattr(
        preview_api,
        "get_target_connection",
        lambda _db, s=None: _Conn(row, ["id", "c"], [(1, "v")]),
    )
    monkeypatch.setattr(
        preview_api,
        "put_target_connection",
        lambda *args: calls.append(args),
    )

    preview_api._preview_table_sync("t", "admin", 100)

    assert calls, "preview did not return its connection"
    returned = calls[0]
    assert returned[0] is not None
    assert returned[2] == schema, (
        f"returned under {returned[2]!r} but checked out under {schema!r}"
    )


def test_preview_opens_the_connection_on_the_tables_path_schema(monkeypatch) -> None:
    """按名读表必须带上该表所属路径的 schema。

    表被归入案件后会物理搬出 ``public``。连接仍按 ``public`` 打开时，预览、
    详情这类入口会以「relation does not exist」报 500 —— 用户看到的是「转移后
    这张表打不开了」。这里锁定 schema 被传下去，且未归类时仍是 ``public``。
    """
    columns = [{"comment": "c", "column_name": "c", "type": "TEXT"}]

    # Unfiled -> the account's public area.
    unfiled = _captured_schema(monkeypatch, _table(columns), ["id", "c"], [(1, "v")])
    preview_api._preview_table_sync("t", "admin", 100)
    assert unfiled == ["public"]

    # Filed into a case -> that path's schema.
    path_id = "11111111-2222-3333-4444-555555555555"
    monkeypatch.setattr(
        preview_api,
        "schema_for_registry_row",
        lambda _row: f"p_{path_id.replace('-', '')}",
    )
    filed = _captured_schema(monkeypatch, _table(columns), ["id", "c"], [(1, "v")])
    preview_api._preview_table_sync("t", "admin", 100)
    assert filed == [f"p_{path_id.replace('-', '')}"]
