# -*- coding: utf-8 -*-
"""Staged import files must be cleaned up completely.

Every upload is staged as ``<UPLOAD_DIR>/<account>/<task_id>/<filename>``. Only
the file used to be removed on success, so the per-job directory stayed behind
and the staging tree grew by one directory per import.

The worse half of the same problem was on the failure path: a job that exhausted
its retries was marked ``failed`` and its staged file was never deleted — and
because the recovery scan only requeues ``pending`` jobs, nothing would ever read
it again. Those files can be up to 10GB each.

These tests pin both, plus the layout guard that keeps the helper from ever
removing an account directory or the uploads root.
"""
from __future__ import annotations

import pathlib

import pytest

from qwenpaw.data_tables import store


@pytest.fixture
def upload_root(tmp_path, monkeypatch) -> pathlib.Path:
    """Repoint the staging root at a temporary directory."""
    root = tmp_path / "data_uploads"
    monkeypatch.setattr(store, "UPLOAD_DIR", str(root))
    return root


def _stage(root: pathlib.Path, account: str = "admin", task: str = "task1"):
    """Create a staged job directory the way an upload does."""
    job_dir = root / account / task
    job_dir.mkdir(parents=True, exist_ok=True)
    staged = job_dir / "data.csv"
    staged.write_text("a,b\n1,2\n")
    return staged, job_dir


def test_success_removes_file_and_directory(upload_root) -> None:
    """The whole per-job directory goes once the import is done."""
    staged, job_dir = _stage(upload_root)

    store.cleanup_staged_file(str(staged))

    assert not staged.exists()
    assert not job_dir.exists(), "empty per-job directory was left behind"


def test_directory_with_other_files_is_preserved(upload_root) -> None:
    """``rmdir`` only succeeds while empty, so a busy directory stays."""
    staged, job_dir = _stage(upload_root)
    other = job_dir / "second.csv"
    other.write_text("still needed")

    store.cleanup_staged_file(str(staged))

    assert not staged.exists()
    assert job_dir.exists(), "directory was removed while it still held a file"
    assert other.exists()


def test_outside_the_job_layout_no_directory_is_removed(upload_root, tmp_path) -> None:
    """A file that is not <root>/<account>/<task>/<file> must not rmdir parents."""
    loose = tmp_path / "elsewhere" / "loose.csv"
    loose.parent.mkdir(parents=True, exist_ok=True)
    loose.write_text("x")
    keep = loose.parent / "sibling.txt"
    keep.write_text("must survive")

    store.cleanup_staged_file(str(loose))

    assert not loose.exists()
    assert loose.parent.exists(), "an unrelated directory was removed"
    assert keep.exists()


def test_account_directory_and_root_are_never_removed(upload_root) -> None:
    """Only the innermost task directory may be deleted."""
    staged, job_dir = _stage(upload_root, account="bob", task="t9")

    store.cleanup_staged_file(str(staged))

    assert not job_dir.exists()
    assert (upload_root / "bob").exists(), "account directory was removed"
    assert upload_root.exists(), "uploads root was removed"


def test_missing_file_is_a_silent_noop(upload_root) -> None:
    """Retrying cleanup (or cleaning an already-gone file) must not raise."""
    store.cleanup_staged_file(str(upload_root / "admin" / "gone" / "x.csv"))
    store.cleanup_staged_file("")


def test_success_path_calls_cleanup() -> None:
    """The importer must use the helper, not a bare unlink."""
    import inspect

    from qwenpaw.data_tables import importer

    source = inspect.getsource(importer.run_import_job)
    assert "cleanup_staged_file(file_path)" in source
    assert "os.remove(file_path)" not in source


def test_exhausted_retries_release_the_staged_file() -> None:
    """A permanently failed job must not keep its upload on disk.

    The recovery scan only requeues ``pending`` jobs, so a ``failed`` job's file
    would otherwise stay forever.
    """
    import inspect

    from qwenpaw.data_tables import importer

    source = inspect.getsource(importer.run_import_job)
    assert "if not retry:" in source
    # Two call sites: the success path and the exhausted-retry path.
    assert source.count("cleanup_staged_file(file_path)") == 2
    # A failure while recording the failure must default to "retry owed", so the
    # file is never dropped before it is last needed.
    assert "retry = True" in source
