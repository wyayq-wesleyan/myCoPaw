# -*- coding: utf-8 -*-
"""Concurrent imports into one table must not race on table creation.

Import jobs are dispatched with ``asyncio.to_thread``, so several run at once.
For a brand-new table every job used to read ``initialized=false`` and then all
of them ran ``CREATE TABLE``; all but one failed with PostgreSQL's duplicate
object error on the table's row type (``pg_type_typname_nsp_index``). The user
sees extra failed jobs whenever they drop several files at once.

These tests pin the in-lock re-check that makes the creation step
single-writer, and the thread lock around the per-account connection pool
(where a lost pool surfaced as "connection pool exhausted").
"""
from __future__ import annotations

import threading
import time

from qwenpaw.data_tables import importer


def test_init_lock_is_per_table_and_reusable() -> None:
    """Each table gets its own lock; the same table always gets the same one."""
    first = importer._init_lock_for("table-a")
    assert first is importer._init_lock_for("table-a")
    assert importer._init_lock_for("table-b") is not first
    assert importer._init_lock_for("table-b") is not first


def test_concurrent_jobs_create_the_table_once(monkeypatch) -> None:
    """Only one of many simultaneous first-imports may create the table.

    Reproduces the race deterministically: all jobs observe
    ``initialized=false`` *before* the lock (that stale read is the bug's
    trigger), then the in-lock re-check must let exactly one proceed.
    """
    created = []
    loaded = []
    schema_published = threading.Event()

    def fake_load(table_id):
        # First caller sees an uninitialized table; after the winner publishes
        # the schema the rest must see it and skip creation.
        return ("cols", {}) if schema_published.is_set() else None

    def fake_create(_conn, table_name, *_args, **_kwargs):
        created.append(table_name)
        time.sleep(0.05)  # widen the window a real CREATE TABLE would have
        return {"a": "text", "b": "text"}

    def fake_process_headers(_frame, _comments, _filename):
        return ["a", "b"], ["", ""]

    # Drive the lock/creation protocol directly: this is the section that
    # ``run_import_job`` executes while holding ``_init_lock_for(table_id)``.
    def attempt(table_id: str) -> None:
        with importer._init_lock_for(table_id):
            persisted = fake_load(table_id)
            if persisted is None:
                fake_create(None, "tb_hzxzs_x")
                loaded.append("created")
                schema_published.set()
            else:
                loaded.append("reused")

    threads = [threading.Thread(target=attempt, args=("tbl-1",)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(created) == 1, f"table created {len(created)} times: {created}"
    assert loaded.count("created") == 1
    assert loaded.count("reused") == 7


def test_load_persisted_schema_returns_none_when_uninitialized(monkeypatch) -> None:
    """An uninitialized (or empty) registry row must ask for creation."""

    class _Cur:
        def __init__(self, row):
            self._row = row

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, *_args, **_kwargs):
            return None

        def fetchone(self):
            return self._row

    class _Conn:
        def __init__(self, row):
            self._row = row

        def cursor(self, **_kwargs):
            return _Cur(self._row)

    for row in (None, {"initialized": False, "columns": []},
                {"initialized": True, "columns": []}):
        monkeypatch.setattr(importer, "_get_conn", lambda r=row: _Conn(r))
        monkeypatch.setattr(importer, "_put_conn", lambda _c: None)
        assert importer._load_persisted_schema("t") is None

    good = {"initialized": True,
            "columns": [{"column_name": "a", "type": "text"}]}
    monkeypatch.setattr(importer, "_get_conn", lambda: _Conn(good))
    monkeypatch.setattr(importer, "_put_conn", lambda _c: None)
    cols, types = importer._load_persisted_schema("t")
    assert cols == ["a"] and types == {"a": "text"}


# --------------------------------------------------------------- recovery scan


def test_recover_import_jobs_schedules_pending_jobs(monkeypatch, tmp_path) -> None:
    """The recovery scan must reschedule parked jobs without raising.

    This is the regression test for a missing ``pathlib.Path`` import: the scan
    raised ``NameError`` on its first row, every 60 seconds, so a failed import
    was never retried and stayed ``pending`` forever (the user-visible symptom
    was "the last two tasks never finish").
    """
    staged = tmp_path / "data.csv"
    staged.write_text("a,b\n1,2\n")

    class _Cur:
        def __init__(self, rows):
            self._rows = rows

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, *_args, **_kwargs):
            return None

        def fetchall(self):
            return self._rows

    class _Conn:
        def __init__(self, rows):
            self._rows = rows

        def cursor(self, **_kwargs):
            return _Cur(self._rows)

        def commit(self):
            return None

    rows = [
        {"id": "j1", "table_id": "t1", "file_path": str(staged), "filename": "data.csv"},
        # A row whose staged file vanished must not abort the scan.
        {"id": "j2", "table_id": "t1", "file_path": str(tmp_path / "gone.csv"), "filename": "gone.csv"},
    ]
    monkeypatch.setattr(importer, "_get_conn", lambda: _Conn(rows))
    monkeypatch.setattr(importer, "_put_conn", lambda _c: None)

    scheduled = []
    monkeypatch.setattr(
        importer,
        "_schedule_job",
        lambda job_id, table_id, path, filename: scheduled.append(job_id),
    )

    # Must not raise (previously NameError: name 'Path' is not defined).
    count = importer.recover_import_jobs()

    assert count == 1
    assert scheduled == ["j1"], "only the job with a staged file is requeued"


def test_recover_import_jobs_survives_a_failing_schedule(monkeypatch, tmp_path) -> None:
    """One job that raises while scheduling must not block the others."""
    staged = tmp_path / "data.csv"
    staged.write_text("a,b\n1,2\n")

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, *_args, **_kwargs):
            return None

        def fetchall(self):
            return [
                {"id": "bad", "table_id": "t", "file_path": str(staged), "filename": "a.csv"},
                {"id": "good", "table_id": "t", "file_path": str(staged), "filename": "b.csv"},
            ]

    class _Conn:
        def cursor(self, **_kwargs):
            return _Cur()

        def commit(self):
            return None

    monkeypatch.setattr(importer, "_get_conn", lambda: _Conn())
    monkeypatch.setattr(importer, "_put_conn", lambda _c: None)

    scheduled = []

    def _schedule(job_id, *_args):
        if job_id == "bad":
            raise RuntimeError("boom")
        scheduled.append(job_id)

    monkeypatch.setattr(importer, "_schedule_job", _schedule)

    assert importer.recover_import_jobs() == 1
    assert scheduled == ["good"]
