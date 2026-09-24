# -*- coding: utf-8 -*-
"""Unit tests for the token usage core module."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from qwenpaw.token_usage.buffer import (
    TokenUsageBuffer,
    _UsageEvent,
    _apply_event,
)
from qwenpaw.token_usage.manager import (
    TokenUsageByDateModel,
    TokenUsageByModel,
    TokenUsageManager,
    TokenUsageRecord,
    TokenUsageStats,
    TokenUsageSummary,
)
from qwenpaw.token_usage.model_wrapper import TokenRecordingModelWrapper
from qwenpaw.platform_store import get_conn, put_conn
from qwenpaw.token_usage.storage import load_data, save_data_sync


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _isolate_token_usage_manager():
    """Isolate token usage manager singleton for each test."""
    # pylint: disable=protected-access
    TokenUsageManager._instance = None
    yield
    TokenUsageManager._instance = None


@pytest.fixture(autouse=True)
def _isolate_token_usage_store():
    """Empty the shared usage table around each test.

    Usage now lives in the platform PostgreSQL store, which has no
    per-workspace dimension to key a throwaway row on — the table IS the global
    totals. Several tests assert on a *fresh* deployment ("no data yet"), and
    others write rows, so the table is snapshotted, cleared before each test, and
    restored after. That keeps assertions deterministic without risking a real
    deployment's usage data on a machine where the suite runs against a live
    database.
    """
    try:
        conn = get_conn()
    except Exception:  # noqa: BLE001 - no database: tests skip on their own
        yield
        return

    snapshot: list[tuple] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT used_on, provider_id, model_name, user_id, "
                "prompt_tokens, completion_tokens, call_count "
                "FROM platform.token_usage",
            )
            snapshot = list(cur.fetchall())
            cur.execute("DELETE FROM platform.token_usage")
        conn.commit()
    finally:
        put_conn(conn)

    yield

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM platform.token_usage")
            for row in snapshot:
                cur.execute(
                    "INSERT INTO platform.token_usage "
                    "(used_on, provider_id, model_name, user_id, "
                    "prompt_tokens, completion_tokens, call_count) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    row,
                )
        conn.commit()
    except Exception:  # noqa: BLE001 - restore must not fail the run
        conn.rollback()
    finally:
        put_conn(conn)


# =============================================================================
# Test _apply_event
# =============================================================================


class TestApplyEvent:
    """Test the _apply_event function that accumulates usage events."""

    # pylint: disable=protected-access

    def test_apply_event_creates_new_entry(self):
        """Should create new entry for first event."""
        cache = {}
        event = _UsageEvent(
            provider_id="openai",
            model_name="gpt-4",
            prompt_tokens=100,
            completion_tokens=50,
            date_str="2026-04-24",
            now_iso="2026-04-24T10:00:00+00:00",
        )
        _apply_event(cache, event)

        assert "2026-04-24" in cache
        assert "openai:gpt-4" in cache["2026-04-24"]
        entry = cache["2026-04-24"]["openai:gpt-4"]
        assert entry["prompt_tokens"] == 100
        assert entry["completion_tokens"] == 50
        assert entry["call_count"] == 1

    def test_apply_event_accumulates_same_model(self):
        """Should accumulate tokens for same provider:model on same date."""
        cache = {}
        for _ in range(3):
            _apply_event(
                cache,
                _UsageEvent(
                    provider_id="openai",
                    model_name="gpt-4",
                    prompt_tokens=100,
                    completion_tokens=50,
                    date_str="2026-04-24",
                    now_iso="2026-04-24T10:00:00+00:00",
                ),
            )

        entry = cache["2026-04-24"]["openai:gpt-4"]
        assert entry["prompt_tokens"] == 300
        assert entry["call_count"] == 3


# =============================================================================
# Test Storage
# =============================================================================


class TestStorage:
    """Storage now persists to the platform PostgreSQL store.

    The document shape is unchanged, so these exercise the same round-trip the
    JSON file used to provide: what ``save_data_sync`` writes, ``load_data``
    must read back.
    """

    @pytest.mark.asyncio
    async def test_load_data_is_empty_when_nothing_recorded(self):
        """An empty store yields an empty document, not an error."""
        data = await load_data()
        assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_save_then_load_round_trips(self):
        """What is written must come back with the same numbers."""
        document = {
            "2026-04-24": {
                "openai:tu-probe": {
                    "provider_id": "openai",
                    "model_name": "tu-probe",
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "call_count": 2,
                },
            },
        }
        assert save_data_sync(None, document) is True
        loaded = await load_data()
        entry = loaded["2026-04-24"]["openai:tu-probe"]
        assert entry["prompt_tokens"] == 100
        assert entry["completion_tokens"] == 50
        assert entry["call_count"] == 2

    @pytest.mark.asyncio
    async def test_per_account_breakdown_round_trips(self):
        """The account dimension must survive a save/load cycle.

        This is the part the JSON file made awkward (a nested dict that could
        only be read wholesale); it is now one row per account.
        """
        document = {
            "2026-04-25": {
                "openai:tu-probe": {
                    "provider_id": "openai",
                    "model_name": "tu-probe",
                    "prompt_tokens": 30,
                    "completion_tokens": 12,
                    "call_count": 2,
                    "by_user": {
                        "alice": {
                            "prompt_tokens": 20,
                            "completion_tokens": 8,
                            "call_count": 1,
                        },
                        "bob": {
                            "prompt_tokens": 10,
                            "completion_tokens": 4,
                            "call_count": 1,
                        },
                    },
                },
            },
        }
        assert save_data_sync(None, document) is True
        loaded = await load_data()
        entry = loaded["2026-04-25"]["openai:tu-probe"]
        # The per-account split is preserved exactly...
        assert entry["by_user"]["alice"]["prompt_tokens"] == 20
        assert entry["by_user"]["bob"]["prompt_tokens"] == 10
        # ...and the entry totals still equal the sum over accounts, so the
        # pre-existing global readers see unchanged numbers.
        assert entry["prompt_tokens"] == 30
        assert entry["completion_tokens"] == 12
        assert entry["call_count"] == 2

    @pytest.mark.asyncio
    async def test_save_does_not_lower_a_concurrent_count(self):
        """A stale snapshot must not overwrite a newer, larger count.

        Two writers can hold snapshots of the same row; the upsert takes the
        maximum, so the larger figure survives instead of the later write
        silently losing the other's increment.
        """
        save_data_sync(None, {
            "2026-04-26": {
                "openai:tu-probe": {
                    "provider_id": "openai",
                    "model_name": "tu-probe",
                    "prompt_tokens": 500,
                    "completion_tokens": 100,
                    "call_count": 5,
                },
            },
        })
        # A stale snapshot with smaller numbers must not undo them.
        save_data_sync(None, {
            "2026-04-26": {
                "openai:tu-probe": {
                    "provider_id": "openai",
                    "model_name": "tu-probe",
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "call_count": 1,
                },
            },
        })
        loaded = await load_data()
        entry = loaded["2026-04-26"]["openai:tu-probe"]
        assert entry["prompt_tokens"] == 500
        assert entry["call_count"] == 5


# =============================================================================
# Test TokenUsageBuffer
# =============================================================================


class TestTokenUsageBuffer:
    """Test TokenUsageBuffer core functionality."""

    # pylint: disable=protected-access

    def test_init_defaults(self, tmp_path):
        """Should initialize with correct defaults."""
        buffer = TokenUsageBuffer(tmp_path / "test.json")
        assert buffer._flush_interval == 10

    @pytest.mark.asyncio
    async def test_enqueue_adds_to_queue(self, tmp_path):
        """Should add event to queue."""
        buffer = TokenUsageBuffer(tmp_path / "test.json")
        event = _UsageEvent(
            provider_id="openai",
            model_name="gpt-4",
            prompt_tokens=100,
            completion_tokens=50,
            date_str="2026-04-24",
            now_iso="2026-04-24T10:00:00+00:00",
        )
        buffer.enqueue(event)
        assert buffer._queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_consumer_processes_events(self, tmp_path):
        """Consumer should process and accumulate events."""
        buffer = TokenUsageBuffer(tmp_path / "test.json")
        buffer.start()

        for _ in range(3):
            buffer.enqueue(
                _UsageEvent(
                    provider_id="openai",
                    model_name="gpt-4",
                    prompt_tokens=100,
                    completion_tokens=50,
                    date_str="2026-04-24",
                    now_iso="2026-04-24T10:00:00+00:00",
                ),
            )

        await asyncio.sleep(0.2)
        await buffer.stop()

        entry = buffer._disk_cache["2026-04-24"]["openai:gpt-4"]
        assert entry["prompt_tokens"] == 300
        assert entry["call_count"] == 3

    @pytest.mark.asyncio
    async def test_stop_does_not_wipe_history_when_seed_interrupted(
        self,
        tmp_path,
        monkeypatch,
    ):
        """A stop() that races cache seeding must not clobber stored history.

        The write path is a cumulative snapshot, so a stop() that runs while
        seeding is still in flight (``_disk_cache`` empty) must NOT persist —
        otherwise it would look like every count dropped to zero.
        """
        existing = {
            "openai": {
                "model_name": "gpt-4",
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "call_count": 1,
            },
        }
        assert save_data_sync(None, {"2026-04-24": {"openai:gpt-4": {
            "provider_id": "openai",
            "model_name": "gpt-4",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "call_count": 1,
        }}}) is True

        seeding = asyncio.Event()

        async def _never_returns(_path=None):
            # Park the consumer inside the seed so stop() runs while
            # ``_disk_cache`` is still the initial empty dict.
            seeding.set()
            await asyncio.Event().wait()
            return {}

        monkeypatch.setattr(
            "qwenpaw.token_usage.buffer.load_data",
            _never_returns,
        )

        buffer = TokenUsageBuffer(tmp_path / "test.json", flush_interval=3600)
        buffer.start()
        await asyncio.wait_for(seeding.wait(), timeout=1)
        await buffer.stop()

        stored = await load_data()
        assert stored["2026-04-24"]["openai:gpt-4"]["prompt_tokens"] == 100
        assert stored["2026-04-24"]["openai:gpt-4"]["call_count"] == 1
        del existing  # documented shape above; asserted via the store

    @pytest.mark.asyncio
    async def test_stop_flushes_after_seed_completes(self, tmp_path):
        """Normal shutdown still merges new events into stored history."""
        assert save_data_sync(None, {"2026-04-23": {"openai:gpt-4": {
            "provider_id": "openai",
            "model_name": "gpt-4",
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "call_count": 1,
        }}}) is True

        buffer = TokenUsageBuffer(tmp_path / "test.json", flush_interval=3600)
        buffer.start()
        buffer.enqueue(
            _UsageEvent(
                provider_id="openai",
                model_name="gpt-4",
                prompt_tokens=100,
                completion_tokens=50,
                date_str="2026-04-24",
                now_iso="2026-04-24T10:00:00+00:00",
            ),
        )
        await buffer.stop()

        written = await load_data()
        assert written["2026-04-23"]["openai:gpt-4"]["prompt_tokens"] == 7
        assert written["2026-04-24"]["openai:gpt-4"]["prompt_tokens"] == 100

    @pytest.mark.asyncio
    async def test_flush_retries_after_transient_write_failure(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Failed flush must keep dirty so the next flush retries (#6374)."""
        buffer = TokenUsageBuffer(
            tmp_path / "token_usage.json", flush_interval=3600,
        )
        buffer._cache_loaded = True
        buffer._disk_cache = {
            "2026-04-24": {
                "openai:gpt-4": {
                    "provider_id": "openai",
                    "model_name": "gpt-4",
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "call_count": 1,
                },
            },
        }
        buffer._dirty = True

        real_save = buffer.__class__.__module__  # silence unused-import linters
        del real_save
        from qwenpaw.token_usage import storage as storage_mod

        real_save_data = storage_mod.save_data_sync
        calls = {"n": 0}

        def _flaky_save(_path=None, data=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return False  # simulated storage failure
            return real_save_data(_path, data)

        monkeypatch.setattr(
            "qwenpaw.token_usage.buffer.save_data_sync",
            _flaky_save,
        )

        await buffer._flush_once()
        # Still dirty, so the next flush retries rather than dropping the data.
        assert buffer._dirty is True
        assert calls["n"] == 1

        await buffer._flush_once()
        assert buffer._dirty is False
        assert calls["n"] == 2
        stored = await load_data()
        assert stored["2026-04-24"]["openai:gpt-4"]["prompt_tokens"] == 100


class TestTokenUsageStats:
    """Test TokenUsageStats model."""

    def test_default_values(self):
        """Should have zero defaults."""
        stats = TokenUsageStats()
        assert stats.prompt_tokens == 0
        assert stats.completion_tokens == 0
        assert stats.call_count == 0

    def test_custom_values(self):
        """Should accept custom values."""
        stats = TokenUsageStats(
            prompt_tokens=100,
            completion_tokens=50,
            call_count=5,
        )
        assert stats.prompt_tokens == 100
        assert stats.completion_tokens == 50
        assert stats.call_count == 5

    def test_validation_rejects_negative(self):
        """Should reject negative values."""
        with pytest.raises(Exception):
            TokenUsageStats(prompt_tokens=-1)


class TestTokenUsageModels:
    """Test TokenUsage models."""

    def test_create_record(self):
        """Should create record with all fields."""
        record = TokenUsageRecord(
            date="2026-04-24",
            provider_id="openai",
            model="gpt-4",
            prompt_tokens=100,
            completion_tokens=50,
            call_count=3,
        )
        assert record.date == "2026-04-24"
        assert record.provider_id == "openai"
        assert record.model == "gpt-4"

    def test_empty_summary(self):
        """Should create empty summary with defaults."""
        summary = TokenUsageSummary()
        assert summary.total_prompt_tokens == 0
        assert summary.total_completion_tokens == 0
        assert summary.total_calls == 0
        assert summary.by_model == {}
        assert summary.by_date == {}

    def test_summary_with_data(self):
        """Should accept populated data."""
        summary = TokenUsageSummary(
            total_prompt_tokens=500,
            total_completion_tokens=250,
            total_calls=10,
            by_model={
                "openai:gpt-4": TokenUsageByModel(
                    provider_id="openai",
                    model="gpt-4",
                    prompt_tokens=500,
                    completion_tokens=250,
                    call_count=10,
                ),
            },
            by_date={
                "2026-04-24": TokenUsageStats(
                    prompt_tokens=500,
                    completion_tokens=250,
                    call_count=10,
                ),
            },
        )
        assert summary.total_prompt_tokens == 500
        assert len(summary.by_model) == 1
        assert summary.by_model["openai:gpt-4"].model == "gpt-4"
        assert len(summary.by_date) == 1

    def test_token_usage_by_model(self):
        """Should create TokenUsageByModel with provider_id."""
        by_model = TokenUsageByModel(
            provider_id="openai",
            model="gpt-4",
            prompt_tokens=300,
            completion_tokens=150,
            call_count=6,
        )
        assert by_model.provider_id == "openai"
        assert by_model.model == "gpt-4"

    def test_token_usage_by_date_model(self):
        """Should create TokenUsageByDateModel."""
        by_date_model = TokenUsageByDateModel(
            provider_id="dashscope",
            model="qwen3-max",
            prompt_tokens=200,
            completion_tokens=100,
            call_count=4,
        )
        assert by_date_model.provider_id == "dashscope"
        assert by_date_model.model == "qwen3-max"


# =============================================================================
# Test TokenUsageManager
# =============================================================================


class TestTokenUsageManagerCore:
    """Test TokenUsageManager singleton, lifecycle, and operations."""

    def test_get_instance_returns_singleton(self, tmp_path, monkeypatch):
        """Should return same instance on multiple calls."""
        monkeypatch.setattr(
            "qwenpaw.token_usage.manager.WORKING_DIR",
            tmp_path,
        )
        monkeypatch.setattr(
            "qwenpaw.token_usage.manager.TOKEN_USAGE_FILE",
            "test_token_usage.json",
        )

        manager1 = TokenUsageManager.get_instance()
        manager2 = TokenUsageManager.get_instance()
        assert manager1 is manager2

    @pytest.mark.asyncio
    async def test_start_and_stop(self, tmp_path, monkeypatch):
        """Should start and stop cleanly."""
        monkeypatch.setattr(
            "qwenpaw.token_usage.manager.WORKING_DIR",
            tmp_path,
        )
        monkeypatch.setattr(
            "qwenpaw.token_usage.manager.TOKEN_USAGE_FILE",
            "test_token_usage.json",
        )

        manager = TokenUsageManager()
        manager.start(flush_interval=10)
        await manager.stop()

    @pytest.mark.asyncio
    async def test_record_usage(self, tmp_path, monkeypatch):
        """Should record token usage."""
        monkeypatch.setattr(
            "qwenpaw.token_usage.manager.WORKING_DIR",
            tmp_path,
        )
        monkeypatch.setattr(
            "qwenpaw.token_usage.manager.TOKEN_USAGE_FILE",
            "test_token_usage.json",
        )

        manager = TokenUsageManager()
        manager.start(flush_interval=10)

        await manager.record(
            provider_id="openai",
            model_name="gpt-4",
            prompt_tokens=100,
            completion_tokens=50,
        )

        await asyncio.sleep(0.2)
        await manager.stop()

    @pytest.mark.asyncio
    async def test_get_summary_empty(self, tmp_path, monkeypatch):
        """Should return empty summary when no data."""
        monkeypatch.setattr(
            "qwenpaw.token_usage.manager.WORKING_DIR",
            tmp_path,
        )
        monkeypatch.setattr(
            "qwenpaw.token_usage.manager.TOKEN_USAGE_FILE",
            "test_token_usage.json",
        )

        manager = TokenUsageManager()
        manager.start(flush_interval=10)

        summary = await manager.get_summary()

        assert summary.total_prompt_tokens == 0
        assert summary.total_completion_tokens == 0
        assert summary.total_calls == 0
        assert summary.by_date == {}

        await manager.stop()

    @pytest.mark.asyncio
    async def test_get_details_empty(self, tmp_path, monkeypatch):
        """Should return empty list when no data."""
        monkeypatch.setattr(
            "qwenpaw.token_usage.manager.WORKING_DIR",
            tmp_path,
        )
        monkeypatch.setattr(
            "qwenpaw.token_usage.manager.TOKEN_USAGE_FILE",
            "test_token_usage.json",
        )

        manager = TokenUsageManager()
        manager.start(flush_interval=10)

        details = await manager.get_details()

        assert details == []

        await manager.stop()

    @pytest.mark.asyncio
    async def test_get_details_with_data(self, tmp_path, monkeypatch):
        """Should return raw records for frontend aggregation."""
        monkeypatch.setattr(
            "qwenpaw.token_usage.manager.WORKING_DIR",
            tmp_path,
        )
        monkeypatch.setattr(
            "qwenpaw.token_usage.manager.TOKEN_USAGE_FILE",
            "test_token_usage.json",
        )

        manager = TokenUsageManager()
        manager.start(flush_interval=10)

        # Record some usage
        await manager.record(
            provider_id="openai",
            model_name="gpt-4",
            prompt_tokens=100,
            completion_tokens=50,
        )
        await manager.record(
            provider_id="dashscope",
            model_name="qwen3-max",
            prompt_tokens=200,
            completion_tokens=100,
        )

        await asyncio.sleep(0.2)

        details = await manager.get_details()

        # Should have 2 records
        assert len(details) == 2

        # Verify structure
        models = {r.model for r in details}
        assert "gpt-4" in models
        assert "qwen3-max" in models

        await manager.stop()


# =============================================================================
# Test TokenRecordingModelWrapper
# =============================================================================


class TestTokenRecordingModelWrapper:
    """Test TokenRecordingModelWrapper."""

    # pylint: disable=protected-access

    def test_init_wraps_model(self, tmp_path, monkeypatch):
        """Should wrap a ChatModelBase instance."""
        monkeypatch.setattr(
            "qwenpaw.token_usage.manager.WORKING_DIR",
            tmp_path,
        )
        monkeypatch.setattr(
            "qwenpaw.token_usage.manager.TOKEN_USAGE_FILE",
            "test_token_usage.json",
        )

        mock_model = MagicMock()
        mock_model.model = "gpt-4"

        wrapper = TokenRecordingModelWrapper(
            provider_id="openai",
            model=mock_model,
        )

        assert wrapper._provider_id == "openai"
        assert wrapper._model is mock_model
        assert wrapper.model == "gpt-4"

    def test_record_usage_with_valid_usage(self, tmp_path, monkeypatch):
        """Should record valid usage."""
        monkeypatch.setattr(
            "qwenpaw.token_usage.manager.WORKING_DIR",
            tmp_path,
        )
        monkeypatch.setattr(
            "qwenpaw.token_usage.manager.TOKEN_USAGE_FILE",
            "test_token_usage.json",
        )

        mock_model = MagicMock()
        mock_model.model = "gpt-4"

        wrapper = TokenRecordingModelWrapper(
            provider_id="openai",
            model=mock_model,
        )

        mock_usage = MagicMock()
        mock_usage.input_tokens = 100
        mock_usage.output_tokens = 50

        wrapper._record_usage(mock_usage)

    def test_record_usage_includes_context_and_threshold(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Per-call usage carries context_size and compaction threshold."""
        monkeypatch.setattr(
            "qwenpaw.token_usage.manager.WORKING_DIR",
            tmp_path,
        )
        monkeypatch.setattr(
            "qwenpaw.token_usage.manager.TOKEN_USAGE_FILE",
            "test_token_usage.json",
        )
        monkeypatch.setattr(
            "qwenpaw.app.agent_context.get_current_session_id",
            lambda: "sess-1",
        )

        mock_model = MagicMock()
        mock_model.model = "gpt-4"
        mock_model.context_size = 1_000_000

        wrapper = TokenRecordingModelWrapper(
            provider_id="openai",
            model=mock_model,
            compact_threshold=0.8,
        )

        mock_usage = MagicMock()
        mock_usage.input_tokens = 123_000
        mock_usage.output_tokens = 50
        wrapper._record_usage(mock_usage)

        stored = TokenRecordingModelWrapper.pop_usage_for_session("sess-1")
        assert stored is not None
        assert stored["context_size"] == 1_000_000
        assert stored["compact_threshold"] == 0.8

    def test_pop_usage_for_session(self, monkeypatch):
        """Should pop usage for session."""
        monkeypatch.setattr(
            "qwenpaw.token_usage.manager.WORKING_DIR",
            "/tmp",
        )
        monkeypatch.setattr(
            "qwenpaw.token_usage.manager.TOKEN_USAGE_FILE",
            "test_token_usage.json",
        )

        # Clear any existing usage
        TokenRecordingModelWrapper._usage_by_session.clear()

        # Add test usage
        TokenRecordingModelWrapper._usage_by_session["test-session"] = {
            "prompt_tokens": 100,
        }

        usage = TokenRecordingModelWrapper.pop_usage_for_session(
            "test-session",
        )
        assert usage is not None
        assert usage["prompt_tokens"] == 100

        # Verify it was removed
        assert (
            TokenRecordingModelWrapper.pop_usage_for_session("test-session")
            is None
        )
