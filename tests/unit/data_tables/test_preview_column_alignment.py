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
        lambda _db: _Conn(table_row, col_names, rows),
    )
    monkeypatch.setattr(preview_api, "put_target_connection", lambda *_a: None)


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
