# -*- coding: utf-8 -*-
"""Large imports land through a single COPY path with whole-batch atomicity.

The old writer walked every row with ``iterrows`` and issued a one-row INSERT per
row via ``execute_batch`` (which the psycopg2 implementation joins into a giant
multi-statement command), committing every 1000 rows. For a ~1M-row import that
is ~1M statements and ~1000 commits; the only thing keeping it alive was that the
file was small enough to finish at all.

These tests pin the replacement: per-column vectorised serialisation into COPY
CSV text, one COPY per chunk, and a single transaction — any error rolls the whole
load back so the target table never holds a partial batch.
"""
from __future__ import annotations

import psycopg2
import pandas as pd
import pytest

from qwenpaw.data_tables import importer


# --------------------------------------------------------------------------- #
# Serialisation: one function per declared type, strict on bad values.
# --------------------------------------------------------------------------- #

def _col(values):
    return pd.Series(values, dtype=object)


def test_text_blanks_become_null_and_values_are_stripped() -> None:
    out = importer._serialize_column(_col(["  a  ", "", None]), "TEXT")
    assert out == ["a", None, None]


def test_integer_accepts_integral_floats_but_rejects_the_rest() -> None:
    assert importer._serialize_column(_col(["1", "2.0", ""]), "INTEGER") == [
        "1",
        "2",
        None,
    ]
    with pytest.raises(ValueError):
        importer._serialize_column(_col(["1", "2.5"]), "INTEGER")
    with pytest.raises(ValueError):
        importer._serialize_column(_col(["1", "abc"]), "BIGINT")


def test_numeric_is_passed_through_without_float_rounding() -> None:
    """A 20-digit NUMERIC must not be turned into a lossy float."""
    exact = "12345678901234567.89"
    out = importer._serialize_column(_col([exact, ""]), "NUMERIC(20,4)")
    assert out == [exact, None]


def test_numeric_rejects_non_numeric() -> None:
    with pytest.raises(ValueError):
        importer._serialize_column(_col(["1.2", "x"]), "NUMERIC(20,4)")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", "true"),
        ("1", "true"),
        ("是", "true"),
        ("Y", "true"),
        ("false", "false"),
        ("0", "false"),
        ("否", "false"),
        ("NO", "false"),
    ],
)
def test_boolean_recognised_values(raw: str, expected: str) -> None:
    assert importer._serialize_column(_col([raw]), "BOOLEAN") == [expected]


def test_boolean_rejects_unknown_value() -> None:
    with pytest.raises(ValueError):
        importer._serialize_column(_col(["maybe"]), "BOOLEAN")


def test_date_accepts_the_known_formats_and_rejects_junk() -> None:
    out = importer._serialize_column(
        _col(["2026-09-23", "2026/09/23", "20260923", "2026年09月23日", ""]),
        "DATE",
    )
    assert out == ["2026-09-23"] * 4 + [None]
    with pytest.raises(ValueError):
        importer._serialize_column(_col(["not-a-date"]), "DATE")


def test_timestamp_accepts_the_known_formats_and_rejects_junk() -> None:
    out = importer._serialize_column(
        _col(["2026-09-23 12:00:00", "2026/09/23 12:00:00", ""]),
        "TIMESTAMP",
    )
    assert out == ["2026-09-23 12:00:00", "2026-09-23 12:00:00", None]
    with pytest.raises(ValueError):
        importer._serialize_column(_col(["2026-09-23"]), "TIMESTAMP")


def test_frame_serialises_to_copy_csv_with_null_and_quoting() -> None:
    frame = pd.DataFrame({"a": ["x,y", ""], "b": ["1", "2"]})
    text = importer._frame_to_csv_text(frame, ["a", "b"], ["TEXT", "INTEGER"])
    assert text == '"x,y",1\n,2\n'


# --------------------------------------------------------------------------- #
# COPY driver: one COPY per chunk, one commit, rollback on any failure.
# --------------------------------------------------------------------------- #

class _FakeCursor:
    def __init__(self, fail_on_call: int | None = None):
        self.calls: list[tuple[str, str]] = []
        self._fail_on = fail_on_call

    def copy_expert(self, sql: str, buf) -> None:
        self.calls.append((sql, buf.read()))
        if self._fail_on is not None and len(self.calls) == self._fail_on:
            raise psycopg2.OperationalError("boom")

    def close(self) -> None:
        pass


class _FakeConn:
    def __init__(self, fail_on_call: int | None = None):
        self._cursor = _FakeCursor(fail_on_call)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _frame(n: int) -> pd.DataFrame:
    return pd.DataFrame({"a": [str(i) for i in range(n)]})


def test_copy_uses_quoted_copy_from_stdin_csv() -> None:
    conn = _FakeConn()
    frame = pd.DataFrame({"a": ["1"], "b": ["2"]})
    importer._copy_insert_frames(conn, "t", ["a", "b"], [frame], ["TEXT", "TEXT"])
    sql, _ = conn._cursor.calls[0]
    assert sql == 'COPY "t" ("a", "b") FROM STDIN WITH (FORMAT csv)'


def test_all_chunks_share_one_commit() -> None:
    conn = _FakeConn()
    total = importer._copy_insert_frames(
        conn, "t", ["a"], [_frame(2), _frame(3)], ["TEXT"],
    )
    assert len(conn._cursor.calls) == 2
    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert total == 5


def test_a_failed_chunk_rolls_the_whole_load_back() -> None:
    conn = _FakeConn(fail_on_call=2)
    with pytest.raises(psycopg2.OperationalError):
        importer._copy_insert_frames(
            conn, "t", ["a"], [_frame(1), _frame(1), _frame(1)], ["TEXT"],
        )
    assert conn.commits == 0
    assert conn.rollbacks == 1


def test_progress_reports_cumulative_rows_after_each_chunk() -> None:
    conn = _FakeConn()
    seen: list[int] = []
    importer._copy_insert_frames(
        conn, "t", ["a"], [_frame(2), _frame(3), _frame(4)], ["TEXT"],
        on_progress=seen.append,
    )
    assert seen == [2, 5, 9]


# --------------------------------------------------------------------------- #
# The job itself must use the COPY path and never the removed row writer.
# --------------------------------------------------------------------------- #

def test_run_import_job_copies_and_drops_the_row_writer() -> None:
    import inspect

    source = inspect.getsource(importer.run_import_job)
    assert "_copy_insert_frames(" in source
    assert "_batch_insert(" not in source
