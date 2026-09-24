# -*- coding: utf-8 -*-
# pylint: disable=too-few-public-methods,protected-access
"""The scroll switch-over notices.

Scroll became the DEFAULT context strategy, so agents that never set
``strategy`` are switched to it silently. ``build_scroll_components`` must log
one notice per workspace per process so the switch and its rollback path are
discoverable. The store now lives in PostgreSQL, so there is no created file to
key on — the dedupe set is process-level.
"""

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import qwenpaw.agents.context as context_mod
import qwenpaw.agents.context.scroll.manager as scroll_manager_mod
from qwenpaw.agents.context import build_scroll_components
from qwenpaw.config.config import LightContextConfig


class _DummyModel:
    """A stand-in model; scroll only stores it, never calls it at wiring."""


def _agent_config(strategy: str = "scroll") -> SimpleNamespace:
    lcc = LightContextConfig(strategy=strategy)
    return SimpleNamespace(running=SimpleNamespace(light_context_config=lcc))


def _build(workspace: Path):
    return build_scroll_components(
        agent_config=_agent_config(),
        workspace_dir=str(workspace),
        model=_DummyModel(),
        session_id="s1",
        agent_id="ag1",
    )


def _notice_records(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "DEFAULT context strategy" in r.msg
    ]


@pytest.fixture(autouse=True)
def _clear_notice_dedupe():
    """Each test starts with a clean process-level notice set."""
    saved = set(context_mod._FIRST_RUN_WARNED)
    context_mod._FIRST_RUN_WARNED.clear()
    yield
    context_mod._FIRST_RUN_WARNED.clear()
    context_mod._FIRST_RUN_WARNED.update(saved)


def test_removed_eviction_headline_settings_are_ignored():
    config = LightContextConfig(
        strategy="scroll",
        scroll_config={
            "summarize_unheadlined_evictions": True,
            "summarize_eviction_timeout_seconds": 45,
        },
    )
    assert not hasattr(
        config.scroll_config,
        "summarize_unheadlined_evictions",
    )
    assert not hasattr(
        config.scroll_config,
        "summarize_eviction_timeout_seconds",
    )


def test_removed_repl_settings_are_ignored():
    """The REPL knobs are gone; a legacy config that still sets them loads
    without error and simply drops them."""
    config = LightContextConfig(
        strategy="scroll",
        scroll_config={"repl_timeout_s": 30, "allow_unsandboxed": True},
    )
    assert not hasattr(config.scroll_config, "repl_timeout_s")
    assert not hasattr(config.scroll_config, "allow_unsandboxed")


@pytest.mark.usefixtures("capture_qwenpaw_logs")
def test_first_run_logs_notice_once(tmp_path: Path, caplog):
    with caplog.at_level(logging.WARNING, logger="qwenpaw.agents.context"):
        components = _build(tmp_path)
    # Scroll actually wired.
    assert components is not None
    # Exactly one notice, naming the workspace handle and the rollback path.
    records = _notice_records(caplog)
    assert len(records) == 1
    msg = records[0].getMessage()
    assert str(tmp_path / "history.db") in msg
    assert "native" in msg


def test_notice_does_not_repeat_in_the_same_process(tmp_path: Path, caplog):
    _build(tmp_path)

    caplog.clear()  # drop the first build's notice; only watch the second
    with caplog.at_level(logging.WARNING, logger="qwenpaw.agents.context"):
        _build(tmp_path)
    assert _notice_records(caplog) == []


def test_notice_fires_for_a_different_workspace(tmp_path: Path, caplog):
    """Dedupe is per workspace, not global: a second workspace still gets told."""
    _build(tmp_path / "one")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="qwenpaw.agents.context"):
        _build(tmp_path / "two")
    assert len(_notice_records(caplog)) == 1


def _size_records(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "history_retention_days" in r.msg
    ]


@pytest.mark.usefixtures("capture_qwenpaw_logs")
def test_large_legacy_db_warns_about_retention(tmp_path: Path, caplog):
    """A leftover pre-migration ``history.db`` still triggers the retention
    warning — that is the file the warning is about."""
    _build(tmp_path)
    db = tmp_path / "history.db"
    db.write_bytes(b"x" * 64)  # stand in for a legacy store
    assert db.exists()

    # Drop the warn threshold below the file so the next build trips it, and
    # clear the process-level dedupe set so the test is order-independent.
    orig = context_mod._DB_SIZE_WARN_BYTES
    context_mod._DB_SIZE_WARN_BYTES = db.stat().st_size - 1
    context_mod._DB_SIZE_WARNED.discard(str(db))
    try:
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="qwenpaw.agents.context"):
            _build(tmp_path)
            records = _size_records(caplog)
            assert len(records) == 1
            assert "history_retention_days" in records[0].getMessage()
            # Deduped: a second build in the same process does not re-warn.
            caplog.clear()
            _build(tmp_path)
            assert _size_records(caplog) == []
    finally:
        context_mod._DB_SIZE_WARN_BYTES = orig
        context_mod._DB_SIZE_WARNED.discard(str(db))


def test_no_notice_when_strategy_is_native(tmp_path: Path, caplog):
    with caplog.at_level(logging.WARNING, logger="qwenpaw.agents.context"):
        components = build_scroll_components(
            agent_config=_agent_config("native"),
            workspace_dir=str(tmp_path),
            model=_DummyModel(),
            session_id="s1",
            agent_id="ag1",
        )
    # Native is unaffected: nothing wired, no notice.
    assert components is None
    assert _notice_records(caplog) == []


def test_wiring_failure_closes_history_store(tmp_path: Path, monkeypatch):
    histories = []

    def fail_manager(**kwargs):
        histories.append(kwargs["history"])
        raise RuntimeError("wiring failed")

    monkeypatch.setattr(
        scroll_manager_mod,
        "ScrollContextManager",
        fail_manager,
    )

    assert _build(tmp_path) is None
    assert len(histories) == 1
    assert histories[0].closed is True
