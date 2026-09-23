# -*- coding: utf-8 -*-
"""Column types default to TEXT so identifiers survive import.

Inferring a type from a sample of values silently broke the case that matters
most: an 11-digit phone number (or 15-digit IMEI/IMSI) matches ``^[+-]?\\d+$``,
was declared INTEGER, and then every insert failed with "integer out of range".
The user-visible result was a table showing "导入次数 9 / 累计导入行数 0" — the
import ran, and not one row landed.

The rule is now: TEXT unless the caller pins a type, with ``AUTO`` available as
an explicit opt-in to the old inference.
"""
from __future__ import annotations

import pandas as pd
import pytest

from qwenpaw.data_tables.importer import _resolve_column_type
from qwenpaw.data_tables.store import ALLOWED_COLUMN_TYPES

PHONE = "13800138000"          # > INTEGER max (2147483647)
IMEI = "860123456789012"


def _frame():
    return pd.DataFrame(
        {
            "用户号码": [PHONE],
            "imei": [IMEI],
            "基站LAC": ["12345"],
            "开始时间": ["2026-09-23 12:00:00"],
            "备注信息": ["备注"],
        },
    )


@pytest.mark.parametrize(
    "column",
    ["用户号码", "imei", "基站LAC", "开始时间", "备注信息"],
)
def test_unpinned_columns_default_to_text(column: str) -> None:
    """No pinned type means TEXT, whatever the values look like."""
    assert _resolve_column_type(_frame(), column, None) == "TEXT"


def test_explicit_auto_opts_into_inference() -> None:
    """AUTO reproduces the old behaviour, per column and only when asked."""
    frame = _frame()
    assert _resolve_column_type(frame, "基站LAC", "AUTO") == "INTEGER"
    assert _resolve_column_type(frame, "开始时间", "AUTO") == "TIMESTAMP"
    # Still wrong for identifiers — which is exactly why it is not the default.
    assert _resolve_column_type(frame, "用户号码", "AUTO") == "INTEGER"


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("BIGINT", "BIGINT"),
        ("bigint", "BIGINT"),
        ("TEXT", "TEXT"),
        ("NUMERIC", "NUMERIC(20,4)"),
        ("boolean", "BOOLEAN"),
        ("DATE", "DATE"),
    ],
)
def test_pinned_type_is_honoured_case_insensitively(
    requested: str,
    expected: str,
) -> None:
    assert _resolve_column_type(_frame(), "用户号码", requested) == expected


def test_unknown_type_falls_back_to_text() -> None:
    """A crafted value must never reach the DDL string."""
    frame = _frame()
    for hostile in ("TEXT); DROP TABLE x; --", "DROP TABLE", "", "  ", "VARCHAR"):
        assert _resolve_column_type(frame, "用户号码", hostile) == "TEXT"


def test_every_allowed_key_resolves_to_a_real_type() -> None:
    """The allowlist maps to DDL types or to the AUTO sentinel, nothing else."""
    frame = _frame()
    for key in ALLOWED_COLUMN_TYPES:
        resolved = _resolve_column_type(frame, "用户号码", key)
        assert isinstance(resolved, str) and resolved, key
        # Resolved values are either a plain type or derived from inference.
        assert resolved.isupper() or "(" in resolved, (key, resolved)


# ------------------------------------------------------------ table update API


def test_update_table_request_is_limited_to_labels() -> None:
    """Only the labels are editable; table_name must not be a field.

    The physical name is already baked into the account's real database table,
    so exposing it here would let a rename orphan the imported data.
    """
    from qwenpaw.data_tables.table_api import UpdateTableRequest

    fields = set(UpdateTableRequest.model_fields)
    assert fields == {"display_name", "description"}


def test_create_request_accepts_optional_column_types() -> None:
    from qwenpaw.data_tables.table_api import CreateTableRequest

    req = CreateTableRequest(
        display_name="t",
        description="d",
        columns=["a", "b"],
        column_types=["BIGINT", "TEXT"],
    )
    assert req.column_types == ["BIGINT", "TEXT"]
    # Omitting them is allowed; every column then defaults to TEXT.
    assert CreateTableRequest(display_name="t", description="d", columns=["a"]).column_types == []


def test_update_route_is_registered() -> None:
    """A PUT on /data-tables/{id} must exist for the console's edit action."""
    from qwenpaw.data_tables.table_api import router

    put_paths = {
        r.path
        for r in router.routes
        if "PUT" in (getattr(r, "methods", set()) or set())
    }
    assert "/data-tables/{table_id}" in put_paths
