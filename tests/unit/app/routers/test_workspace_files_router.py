# -*- coding: utf-8 -*-
"""API tests for the unified Files workspace contract."""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from watchfiles import Change

from qwenpaw.app.routers import workspace as workspace_router


@pytest.fixture(name="files_client")
def fixture_files_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """Create a workspace router client bound to a temporary project."""
    project_dir = tmp_path / "project"
    workspace_dir = tmp_path / "workspace"
    project_dir.mkdir()
    workspace_dir.mkdir()

    async def get_workspace(_request):
        return SimpleNamespace(
            agent_id="files-test",
            workspace_dir=workspace_dir,
        )

    monkeypatch.setattr(
        workspace_router,
        "get_agent_for_request",
        get_workspace,
    )

    async def get_project_dir(_request, _workspace):
        return project_dir

    monkeypatch.setattr(
        workspace_router,
        "get_project_dir_for_request",
        get_project_dir,
    )
    app = FastAPI()
    app.state.project_dir = project_dir
    app.state.workspace_dir = workspace_dir
    app.include_router(workspace_router.router, prefix="/api")
    return TestClient(app)


def test_tree_metadata_and_chunk_contract(
    files_client: TestClient,
) -> None:
    """The API lists one level and reads metadata before bounded content."""
    project_dir = files_client.app.state.project_dir
    (project_dir / "src").mkdir()
    (project_dir / "src" / "app.py").write_text(
        "print('ok')",
        encoding="utf-8",
    )
    (project_dir / "README.md").write_text("Hello", encoding="utf-8")

    tree = files_client.get("/api/workspace/tree").json()
    metadata = files_client.get(
        "/api/workspace/file-metadata",
        params={"path": "README.md"},
    ).json()
    content = files_client.get(
        "/api/workspace/file-content",
        params={"path": "README.md", "offset": 0, "limit": 3},
    ).json()

    assert [entry["name"] for entry in tree["entries"]] == [
        "src",
        "README.md",
    ]
    assert metadata["preview_kind"] == "text"
    assert content["content"] == "Hel"
    assert content["truncated"] is True


def test_inbox_upload_lands_in_the_fixed_upload_directory(
    files_client: TestClient,
) -> None:
    """Every upload goes to <workspace>/upload, regardless of the current root."""
    workspace_dir = files_client.app.state.workspace_dir

    response = files_client.post(
        "/api/workspace/inbox-upload",
        files={"files": ("note.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 200
    payload = response.json()["files"][0]
    assert payload["path"] == "upload/note.txt"
    assert payload["archive"] is None
    assert payload["job_id"] is None
    assert (workspace_dir / "upload" / "note.txt").read_bytes() == b"hello"


def test_inbox_upload_extracts_an_archive_and_reports_the_job(
    files_client: TestClient,
) -> None:
    """An uploaded zip is extracted in the background and stays on disk."""
    workspace_dir = files_client.app.state.workspace_dir
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("data/a.txt", "alpha")
        archive.writestr("data/b.txt", "beta")

    response = files_client.post(
        "/api/workspace/inbox-upload",
        files={"files": ("data.zip", bundle.getvalue(), "application/zip")},
    )

    assert response.status_code == 200
    payload = response.json()["files"][0]
    assert payload["archive"] == "zip"
    assert payload["job_id"]

    job = {}
    for _ in range(200):
        job_response = files_client.get(
            f"/api/workspace/inbox-jobs/{payload['job_id']}",
        )
        assert job_response.status_code == 200
        job = job_response.json()
        if job["status"] in ("ready", "failed"):
            break
        time.sleep(0.02)

    assert job["status"] == "ready", job
    assert job["files"] == 2
    assert job["archive_deleted"] is True
    assert (workspace_dir / "upload" / "data" / "a.txt").read_text(
        encoding="utf-8",
    ) == "alpha"
    assert not (workspace_dir / "upload" / "data.zip").exists(), (
        "a successfully extracted archive is removed"
    )


def test_inbox_upload_reports_partial_failures_without_losing_successes(
    files_client: TestClient,
) -> None:
    """One bad filename must not hide the files that did land."""
    workspace_dir = files_client.app.state.workspace_dir

    response = files_client.post(
        "/api/workspace/inbox-upload",
        files=[
            ("files", ("ok.txt", b"ok", "text/plain")),
            ("files", (".", b"bad", "text/plain")),
        ],
    )

    assert response.status_code == 200
    payload = response.json()
    assert [item["path"] for item in payload["files"]] == ["upload/ok.txt"]
    assert len(payload["errors"]) == 1
    assert (workspace_dir / "upload" / "ok.txt").read_bytes() == b"ok"


def test_inbox_upload_all_failures_return_400(files_client: TestClient) -> None:
    response = files_client.post(
        "/api/workspace/inbox-upload",
        files={"files": (".", b"bad", "text/plain")},
    )

    assert response.status_code == 400
    assert "文件名无效" in response.json()["detail"]


def test_inbox_rename_and_delete_are_limited_to_upload(
    files_client: TestClient,
) -> None:
    workspace_dir = files_client.app.state.workspace_dir
    upload = workspace_dir / "upload"
    upload.mkdir()
    (upload / "old.txt").write_text("old", encoding="utf-8")
    (upload / "taken.txt").write_text("taken", encoding="utf-8")
    (workspace_dir / "secret.txt").write_text("secret", encoding="utf-8")

    renamed = files_client.post(
        "/api/workspace/inbox-rename",
        json={"path": "upload/old.txt", "new_name": "new.txt"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["path"] == "upload/new.txt"
    assert (upload / "new.txt").read_text(encoding="utf-8") == "old"

    collision = files_client.post(
        "/api/workspace/inbox-rename",
        json={"path": "upload/new.txt", "new_name": "taken.txt"},
    )
    assert collision.status_code == 409

    outside = files_client.post(
        "/api/workspace/inbox-rename",
        json={"path": "secret.txt", "new_name": "stolen.txt"},
    )
    assert outside.status_code == 400
    assert (workspace_dir / "secret.txt").is_file()

    deleted = files_client.post(
        "/api/workspace/inbox-delete",
        json={"path": "upload/new.txt"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["freed_bytes"] > 0
    assert not (upload / "new.txt").exists()


def test_inbox_delete_directory_requires_recursive(
    files_client: TestClient,
) -> None:
    workspace_dir = files_client.app.state.workspace_dir
    target = workspace_dir / "upload" / "data"
    target.mkdir(parents=True)
    (target / "a.txt").write_text("a", encoding="utf-8")

    refused = files_client.post(
        "/api/workspace/inbox-delete",
        json={"path": "upload/data"},
    )
    assert refused.status_code == 409
    assert target.is_dir()

    deleted = files_client.post(
        "/api/workspace/inbox-delete",
        json={"path": "upload/data", "recursive": True},
    )
    assert deleted.status_code == 200
    assert not target.exists()


def test_inbox_delete_refuses_the_upload_root(files_client: TestClient) -> None:
    response = files_client.post(
        "/api/workspace/inbox-delete",
        json={"path": "upload"},
    )
    assert response.status_code == 400
    assert (files_client.app.state.workspace_dir / "upload").is_dir()


def test_inbox_batch_delete_removes_selected_entries(
    files_client: TestClient,
) -> None:
    workspace_dir = files_client.app.state.workspace_dir
    upload = workspace_dir / "upload"
    (upload / "dir").mkdir(parents=True)
    (upload / "dir" / "a.txt").write_text("a", encoding="utf-8")
    (upload / "b.txt").write_text("b", encoding="utf-8")

    response = files_client.post(
        "/api/workspace/inbox-delete-batch",
        json={
            "paths": ["upload/dir", "upload/dir/a.txt", "upload/b.txt"],
            "recursive": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["deleted"] == ["upload/dir", "upload/b.txt"]
    assert payload["failed"] == []
    assert payload["freed_bytes"] > 0
    assert not (upload / "dir").exists()
    assert not (upload / "b.txt").exists()


def test_inbox_batch_delete_validates_all_paths_before_deleting(
    files_client: TestClient,
) -> None:
    workspace_dir = files_client.app.state.workspace_dir
    upload = workspace_dir / "upload"
    upload.mkdir()
    (upload / "a.txt").write_text("a", encoding="utf-8")
    (workspace_dir / "secret.txt").write_text("secret", encoding="utf-8")

    response = files_client.post(
        "/api/workspace/inbox-delete-batch",
        json={"paths": ["upload/a.txt", "secret.txt"], "recursive": True},
    )

    assert response.status_code == 400
    assert (upload / "a.txt").is_file()
    assert (workspace_dir / "secret.txt").is_file()


def test_inbox_batch_delete_reports_partial_failures(
    files_client: TestClient,
) -> None:
    workspace_dir = files_client.app.state.workspace_dir
    upload = workspace_dir / "upload"
    upload.mkdir()
    (upload / "a.txt").write_text("a", encoding="utf-8")

    response = files_client.post(
        "/api/workspace/inbox-delete-batch",
        json={
            "paths": ["upload/a.txt", "upload/missing.txt"],
            "recursive": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["deleted"] == ["upload/a.txt"]
    assert len(payload["failed"]) == 1
    assert payload["freed_bytes"] > 0
    assert not (upload / "a.txt").exists()


def test_inbox_batch_delete_rejects_an_empty_selection(
    files_client: TestClient,
) -> None:
    response = files_client.post(
        "/api/workspace/inbox-delete-batch",
        json={"paths": []},
    )
    assert response.status_code == 400


def test_inbox_job_unknown_id_is_404(files_client: TestClient) -> None:
    response = files_client.get(
        "/api/workspace/inbox-jobs/0123456789abcdef0123456789abcdef",
    )
    assert response.status_code == 404


def test_save_uses_if_match_conflict_detection(
    files_client: TestClient,
) -> None:
    """Stale ETags return a conflict instead of overwriting disk state."""
    target = files_client.app.state.project_dir / "notes.md"
    target.write_text("before", encoding="utf-8")
    metadata = files_client.get(
        "/api/workspace/file-metadata",
        params={"path": "notes.md"},
    ).json()

    saved = files_client.put(
        "/api/workspace/file-content",
        params={"path": "notes.md"},
        headers={"If-Match": metadata["etag"]},
        json={"content": "after"},
    )
    stale = files_client.put(
        "/api/workspace/file-content",
        params={"path": "notes.md"},
        headers={"If-Match": metadata["etag"]},
        json={"content": "stale"},
    )

    assert saved.status_code == 200
    assert stale.status_code == 409
    assert target.read_text(encoding="utf-8") == "after"


def test_save_if_match_rejects_an_externally_deleted_file(
    files_client: TestClient,
) -> None:
    """A stale editor cannot recreate a file deleted after it was opened."""
    target = files_client.app.state.project_dir / "notes.md"
    target.write_text("before", encoding="utf-8")
    metadata = files_client.get(
        "/api/workspace/file-metadata",
        params={"path": "notes.md"},
    ).json()
    target.unlink()

    response = files_client.put(
        "/api/workspace/file-content",
        params={"path": "notes.md"},
        headers={"If-Match": metadata["etag"]},
        json={"content": "stale"},
    )

    assert response.status_code == 409
    assert not target.exists()


def test_upload_requests_policy_only_for_conflicting_files(
    files_client: TestClient,
) -> None:
    """Uploads proceed normally and request a policy only on conflict."""
    project_dir = files_client.app.state.project_dir
    (project_dir / "report.txt").write_text("old", encoding="utf-8")

    uploaded = files_client.post(
        "/api/workspace/file-upload",
        files={"files": ("new.txt", b"new", "text/plain")},
    )
    conflict = files_client.post(
        "/api/workspace/file-upload",
        files={"files": ("report.txt", b"new", "text/plain")},
    )
    renamed = files_client.post(
        "/api/workspace/file-upload",
        params={"conflict": "rename"},
        files={"files": ("report.txt", b"new", "text/plain")},
    )

    assert uploaded.status_code == 200
    assert (project_dir / "new.txt").read_text(encoding="utf-8") == "new"
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == {
        "code": "upload_conflict",
        "files": ["report.txt"],
    }
    assert (project_dir / "report.txt").read_text(encoding="utf-8") == "old"
    assert renamed.status_code == 200
    assert (project_dir / "report (1).txt").read_text(
        encoding="utf-8",
    ) == "new"


def test_upload_detects_case_aliases_in_one_batch(
    files_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case-insensitive filesystems request a policy before writing."""
    monkeypatch.setattr(
        workspace_router,
        "_filesystem_name_rules",
        lambda _directory: (False, True),
    )

    response = files_client.post(
        "/api/workspace/file-upload",
        files=[
            ("files", ("A.txt", b"first", "text/plain")),
            ("files", ("a.txt", b"second", "text/plain")),
        ],
    )

    assert response.status_code == 409
    assert response.json()["detail"]["files"] == ["a.txt"]
    assert not (files_client.app.state.project_dir / "A.txt").exists()
    assert not (files_client.app.state.project_dir / "a.txt").exists()


def test_upload_detects_unicode_normalization_aliases(
    files_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normalization-insensitive filesystems reject equivalent batch names."""
    monkeypatch.setattr(
        workspace_router,
        "_filesystem_name_rules",
        lambda _directory: (True, False),
    )

    response = files_client.post(
        "/api/workspace/file-upload",
        files=[
            ("files", ("é.txt", b"first", "text/plain")),
            ("files", ("e\u0301.txt", b"second", "text/plain")),
        ],
    )

    assert response.status_code == 409
    assert response.json()["detail"]["files"] == ["e\u0301.txt"]


def test_download_streams_safe_file_and_rejects_traversal(
    files_client: TestClient,
) -> None:
    """Downloads stream content while traversal remains blocked."""
    project_dir = files_client.app.state.project_dir
    (project_dir / "result.bin").write_bytes(b"\x00\x01")

    response = files_client.get(
        "/api/workspace/file-download",
        params={"path": "result.bin"},
    )
    traversal = files_client.get(
        "/api/workspace/file-download",
        params={"path": "../secret"},
    )

    assert response.status_code == 200
    assert response.content == b"\x00\x01"
    assert response.headers["accept-ranges"] == "bytes"
    assert traversal.status_code == 400


def test_workspace_root_is_independent_from_project_root(
    files_client: TestClient,
) -> None:
    """The workspace selector lists agent configuration files."""
    project_dir = files_client.app.state.project_dir
    workspace_dir = files_client.app.state.workspace_dir
    (project_dir / "project.txt").write_text("project", encoding="utf-8")
    (workspace_dir / "AGENTS.md").write_text("profile", encoding="utf-8")

    project_tree = files_client.get("/api/workspace/tree").json()
    workspace_tree = files_client.get(
        "/api/workspace/tree",
        params={"root": "workspace"},
    ).json()

    assert [entry["name"] for entry in project_tree["entries"]] == [
        "project.txt",
    ]
    assert [entry["name"] for entry in workspace_tree["entries"]] == [
        "AGENTS.md",
    ]


def test_watch_rejects_an_unknown_root(files_client: TestClient) -> None:
    """The watch stream uses the same explicit root contract as file APIs."""
    response = files_client.get(
        "/api/workspace/watch",
        params={"root": "unknown"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "root must be project or workspace"


@pytest.mark.asyncio
async def test_watch_remains_open_after_idle_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle poll must not close the underlying file watcher."""
    watch_dir = tmp_path / "project"
    watch_dir.mkdir()
    target = watch_dir / "notes.md"
    captured: dict[str, object] = {}

    class FakeWatcher:
        def __init__(self) -> None:
            self.calls = 0
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.calls += 1
            if self.calls == 1:
                return set()
            if self.calls == 2:
                return {(Change.modified, str(target))}
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed = True

    watcher = FakeWatcher()

    def fake_awatch(path: Path, **kwargs):
        captured["path"] = path
        captured.update(kwargs)
        return watcher

    class ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    monkeypatch.setattr(workspace_router, "awatch", fake_awatch)
    messages = [
        message
        async for message in workspace_router.workspace_watch_events(
            ConnectedRequest(),
            watch_dir,
        )
    ]

    assert captured == {
        "path": watch_dir,
        "rust_timeout": 1_000,
        "yield_on_timeout": True,
    }
    assert any('"path": "notes.md"' in message for message in messages)
    assert watcher.calls == 3
    assert watcher.closed is True


@pytest.mark.asyncio
async def test_watch_checks_disconnect_after_idle_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle wake-up must promptly observe a disconnected client."""
    watch_dir = tmp_path / "project"
    watch_dir.mkdir()

    class IdleWatcher:
        def __init__(self) -> None:
            self.calls = 0
            self.closed = False

        async def __anext__(self):
            self.calls += 1
            return set()

        async def aclose(self) -> None:
            self.closed = True

    class DisconnectingRequest:
        def __init__(self) -> None:
            self.calls = 0

        async def is_disconnected(self) -> bool:
            self.calls += 1
            return self.calls > 1

    watcher = IdleWatcher()
    request = DisconnectingRequest()
    monkeypatch.setattr(
        workspace_router,
        "awatch",
        lambda *_args, **_kwargs: watcher,
    )

    messages = [
        message
        async for message in workspace_router.workspace_watch_events(
            request,
            watch_dir,
        )
    ]

    assert messages == ['data: {"type": "connected"}\n\n']
    assert watcher.calls == 1
    assert request.calls == 2
    assert watcher.closed is True
