"""Phase-3: per-account storage locations and one consistent admin rule.

Two things are pinned here:

* Shared storage had no account component, so records were separable only by a
  random id an account would have to learn. Import staging and inbox traces now
  live under an account subtree.
* Admin semantics were inconsistent — data tables and file preview let an admin
  read every account, while knowledge spaces and agent ownership did not. They
  are now uniformly "admins are ordinary accounts" for per-user data, with
  admin-only *management* surfaces (backups, plugins, local models, global
  settings) remaining administrator-gated.
"""

from __future__ import annotations

import asyncio
import inspect
import importlib

import pytest
from fastapi import HTTPException

from qwenpaw.data_tables.store import UPLOAD_DIR, user_upload_dir


# --------------------------------------------------------------- staging path


def test_import_staging_is_per_account():
    assert user_upload_dir("alice") != user_upload_dir("bob")
    assert user_upload_dir("alice").startswith(UPLOAD_DIR)
    assert user_upload_dir("bob").startswith(UPLOAD_DIR)


def test_import_staging_is_stable_for_an_account():
    assert user_upload_dir("alice") == user_upload_dir("alice")


def test_import_staging_handles_a_cjk_username():
    """A Chinese username must not produce an unusable directory name."""
    path = user_upload_dir("张三")

    assert path.startswith(UPLOAD_DIR)
    assert path != user_upload_dir("李四")
    assert all(part.isascii() for part in path.split("/"))


def test_import_staging_falls_back_for_a_blank_account():
    assert user_upload_dir("") == user_upload_dir("   ")


def test_import_api_stages_under_the_account(monkeypatch):
    """The endpoint must use the per-account helper, not the flat root."""
    import_api = importlib.import_module("qwenpaw.data_tables.import_api")
    source = inspect.getsource(import_api.import_data)

    assert "user_upload_dir" in source
    assert "os.path.join(UPLOAD_DIR, task_id)" not in source


# ------------------------------------------------------------------ traces


@pytest.fixture
def trace_store(tmp_path, monkeypatch):
    store = importlib.import_module("qwenpaw.app.inbox_trace_store")
    monkeypatch.setattr(store, "_TRACE_DIR", tmp_path)
    return store


@pytest.mark.asyncio
async def test_traces_are_written_under_the_owner(trace_store, tmp_path):
    await trace_store.create_trace("run-1", meta={}, owner="alice")
    await trace_store.create_trace("run-2", meta={}, owner="bob")

    files = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.json"))
    assert files == ["alice/run-1.json", "bob/run-2.json"]


@pytest.mark.asyncio
async def test_a_trace_is_readable_only_by_its_owner(trace_store):
    await trace_store.create_trace("run-1", meta={}, owner="alice")

    assert await trace_store.get_trace("run-1", requester="alice") is not None
    assert await trace_store.get_trace("run-1", requester="bob") is None
    assert (
        await trace_store.get_trace("run-1", requester="bob", is_admin=True)
        is not None
    )


@pytest.mark.asyncio
async def test_legacy_flat_trace_is_still_found(trace_store, tmp_path):
    """A trace written before the split must not be lost across the upgrade."""
    (tmp_path / "legacy.json").write_text(
        '{"run_id": "legacy", "meta": {}, "owner": "alice", "events": []}',
        encoding="utf-8",
    )

    assert await trace_store.get_trace("legacy", requester="alice") is not None


@pytest.mark.asyncio
async def test_delete_finds_a_trace_wherever_it_lives(trace_store, tmp_path):
    await trace_store.create_trace("run-1", meta={}, owner="alice")

    assert await trace_store.delete_trace("run-1") is True
    assert not list(tmp_path.rglob("run-1.json"))


# ------------------------------------------------------- consistent admin rule


@pytest.mark.parametrize(
    "module_path,attr",
    [
        ("qwenpaw.data_tables.table_api", None),
        ("qwenpaw.data_tables.import_api", None),
        ("qwenpaw.data_tables.preview_api", None),
    ],
)
def test_data_tables_have_no_admin_bypass(module_path, attr):
    """Data tables follow the same per-account rule as knowledge spaces."""
    module = importlib.import_module(module_path)
    source = inspect.getsource(module)

    assert "is_admin_user" not in source, module_path


def test_file_preview_confines_admins_too():
    files = importlib.import_module("qwenpaw.app.routers.files")
    source = inspect.getsource(files.preview_file)

    assert "is_admin_user" not in source
    assert "relative_to(get_user_root(username).resolve())" in source


def test_local_models_require_an_administrator():
    """One shared model server and model directory: administrator-only."""
    module = importlib.import_module("qwenpaw.app.routers.local_models")
    guard = importlib.import_module("qwenpaw.app.routers._admin_guard")

    assert guard.require_admin_request in [
        d.dependency for d in module.router.dependencies
    ]


def test_offload_policy_requires_an_administrator():
    settings = importlib.import_module("qwenpaw.app.routers.settings")
    source = inspect.getsource(settings.set_offload_policy)

    assert "require_admin_request" in source


def test_backend_logs_require_an_administrator():
    """The log is shared, so a normal user must not read it."""
    console = importlib.import_module("qwenpaw.app.routers.console")
    source = inspect.getsource(console.get_backend_debug_logs)

    assert "require_admin_request" in source


# ------------------------------------------------------------------- memory


def test_memory_namespace_is_scoped_to_the_account(monkeypatch):
    module = importlib.import_module(
        "qwenpaw.agents.memory.adbpg_memory_manager",
    )
    manager = module.ADBPGMemoryManager.__new__(module.ADBPGMemoryManager)
    manager.agent_id = "user_alice_default"

    monkeypatch.setattr(
        "qwenpaw.app.user_space.owner_for_agent",
        lambda _agent_id: "alice",
    )
    assert manager._resolve_memory_user_id() == "alice"


def test_memory_namespace_never_collapses_to_shared(monkeypatch):
    """An unresolvable owner must still not merge every account."""
    module = importlib.import_module(
        "qwenpaw.agents.memory.adbpg_memory_manager",
    )
    manager = module.ADBPGMemoryManager.__new__(module.ADBPGMemoryManager)
    manager.agent_id = "user_alice_default"

    monkeypatch.setattr(
        "qwenpaw.app.user_space.owner_for_agent",
        lambda _agent_id: None,
    )
    assert manager._resolve_memory_user_id() == "user_alice_default"
