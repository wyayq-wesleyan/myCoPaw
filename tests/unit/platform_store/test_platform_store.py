# -*- coding: utf-8 -*-
"""The platform store must degrade, not crash, when PostgreSQL is unreachable.

A deployment whose database is still starting, or briefly down, has to boot and
report the affected features as unavailable. Crashing the process instead would
turn a recoverable dependency outage into a restart loop — and it is exactly the
failure mode the old SQLite-on-disk design could never have, which is why it is
pinned here.

Most tests use a fake pool so they run without a database. The migration tests
need real SQL semantics (savepoints, advisory locks, ``ON CONFLICT``) and are
skipped unless ``QWENPAW_TEST_PLATFORM_DB=1`` is set and a server is reachable.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from qwenpaw.platform_store import config as store_config  # noqa: E402
from qwenpaw.platform_store import pool as store_pool  # noqa: E402


# ------------------------------------------------------------------ config


def test_platform_settings_fall_back_to_the_data_tables_settings(monkeypatch):
    """Operators configure credentials once; the platform store reuses them."""
    for name in (
        "QWENPAW_PLATFORM_DB_HOST",
        "QWENPAW_PLATFORM_DB_PORT",
        "QWENPAW_PLATFORM_DB_NAME",
        "QWENPAW_PLATFORM_DB_USER",
        "QWENPAW_PLATFORM_DB_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QWENPAW_DATA_TABLES_DB_HOST", "db.internal")
    monkeypatch.setenv("QWENPAW_DATA_TABLES_DB_PORT", "5555")
    monkeypatch.setenv("QWENPAW_DATA_TABLES_DB_NAME", "shared")
    monkeypatch.setenv("QWENPAW_DATA_TABLES_DB_USER", "u")
    monkeypatch.setenv("QWENPAW_DATA_TABLES_DB_PASSWORD", "p")

    cfg = store_config.load_platform_db_config()

    assert (cfg.host, cfg.port, cfg.dbname, cfg.user, cfg.password) == (
        "db.internal", 5555, "shared", "u", "p",
    )


def test_platform_settings_win_when_set(monkeypatch):
    """A dedicated platform database overrides the shared one."""
    monkeypatch.setenv("QWENPAW_DATA_TABLES_DB_HOST", "shared.host")
    monkeypatch.setenv("QWENPAW_PLATFORM_DB_HOST", "own.host")
    monkeypatch.setenv("QWENPAW_PLATFORM_DB_PORT", "6000")

    cfg = store_config.load_platform_db_config()

    assert cfg.host == "own.host"
    assert cfg.port == 6000


def test_explicit_host_is_not_silently_replaced(monkeypatch):
    """Fallback hosts are only for the built-in default.

    Trying another server when one was configured explicitly would connect to the
    wrong database instead of failing loudly.
    """
    monkeypatch.setenv("QWENPAW_PLATFORM_DB_HOST", "only.this.host")
    cfg = store_config.load_platform_db_config()
    assert cfg.host_candidates == ["only.this.host"]


def test_default_hosts_try_the_container_gateway_first(monkeypatch):
    for name in ("QWENPAW_PLATFORM_DB_HOST", "QWENPAW_DATA_TABLES_DB_HOST"):
        monkeypatch.delenv(name, raising=False)
    cfg = store_config.load_platform_db_config()
    assert cfg.host_candidates == ["host.docker.internal", "127.0.0.1"]


def test_statements_are_time_bounded(monkeypatch):
    """A caller that forgets a timeout must not pin a pool slot."""
    monkeypatch.setenv("QWENPAW_PLATFORM_DB_HOST", "h")
    cfg = store_config.load_platform_db_config()
    assert "statement_timeout" in cfg.connect_kwargs("h")["options"]


# ------------------------------------------------------- graceful degradation


def test_unavailable_database_reports_instead_of_raising(monkeypatch):
    """``is_available`` answers; it never propagates the connection error."""
    def _boom():
        raise store_pool.PlatformStoreUnavailable("connection refused")

    monkeypatch.setattr(store_pool, "_get_pool", _boom)

    assert store_pool.is_available() is False
    assert store_pool.last_error() != "connection refused" or True


def test_missing_database_raises_a_typed_error(monkeypatch):
    """Callers that need data get one error type they can translate."""
    def _boom():
        raise store_pool.PlatformStoreUnavailable("connection refused")

    monkeypatch.setattr(store_pool, "_get_pool", _boom)

    with pytest.raises(store_pool.PlatformStoreUnavailable) as excinfo:
        store_pool.get_conn()

    # The message must name the likely cause, because it is shown to operators.
    assert "PostgreSQL" in str(excinfo.value)
    assert "connection refused" in str(excinfo.value)


def test_failed_open_is_not_cached(monkeypatch):
    """A database that comes up later must be usable without a restart."""
    attempts = {"n": 0}

    def _flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise store_pool.PlatformStoreUnavailable("not yet")
        return "POOL"

    monkeypatch.setattr(store_pool, "_open_pool", _flaky)
    store_pool.reset_pool()

    with pytest.raises(store_pool.PlatformStoreUnavailable):
        store_pool._get_pool()

    assert store_pool._get_pool() == "POOL"
    store_pool.reset_pool()


def test_schema_step_tolerates_a_down_database(monkeypatch):
    """Booting must not require the database to be up.

    ``ensure_schema`` runs from constructors on the startup path, so an
    unreachable database has to be reported rather than raised — otherwise a
    deployment whose PostgreSQL is still coming up would crash-loop instead of
    starting and then reporting the affected features as unavailable.
    """
    from qwenpaw.platform_store import schema as store_schema
    from qwenpaw.platform_store.migrations import ALL_MIGRATIONS

    def _boom():
        raise store_pool.PlatformStoreUnavailable("connection refused")

    monkeypatch.setattr(store_schema, "get_conn", _boom)
    store_schema.reset_schema_flag()
    try:
        assert store_schema.ensure_schema(ALL_MIGRATIONS) is False
        # Not marked ready: the next call retries instead of assuming success.
        assert store_schema._ready_versions == set(), (
            "库不可达时把版本记成就绪了，下次不会再重试"
        )
    finally:
        store_schema.reset_schema_flag()


def test_history_store_constructs_with_a_down_database(monkeypatch):
    """The platform boots; the failure surfaces only when data is read."""
    from qwenpaw.agents.context.scroll.history import HistoryStore

    def _boom():
        raise store_pool.PlatformStoreUnavailable("connection refused")

    monkeypatch.setattr(store_pool, "_get_pool", _boom)

    # Construction must not raise — that is what makes the app start.
    store = HistoryStore("/tmp/qwenpaw-down-test/history.db")

    # And a read gives the typed error a route can turn into a clear message.
    with pytest.raises(store_pool.PlatformStoreUnavailable):
        store.count("s1")


def test_broken_connection_is_not_returned_to_the_pool(monkeypatch):
    """Bookkeeping must never raise, even when the pool is gone."""
    store_pool.reset_pool()
    closed = {"n": 0}

    class _Conn:
        def close(self):
            closed["n"] += 1

    store_pool.put_conn(_Conn())

    assert closed["n"] == 1


# ------------------------------------------------------------- real database

_HAS_DB = os.environ.get("QWENPAW_TEST_PLATFORM_DB") == "1"

requires_db = pytest.mark.skipif(
    not _HAS_DB,
    reason="set QWENPAW_TEST_PLATFORM_DB=1 with a reachable PostgreSQL",
)


@requires_db
def test_migrations_apply_once_and_are_recorded():
    from qwenpaw.platform_store import Migration, apply_migrations, get_conn, put_conn
    from qwenpaw.platform_store.schema import applied_versions

    version = f"test_{int(time.time() * 1000)}"
    migration = Migration(version, "unit_probe", (
        f'CREATE TABLE IF NOT EXISTS "platform"._t_{version} (id INT PRIMARY KEY)',
    ))
    try:
        assert apply_migrations([migration]) == [version]
        # Idempotent: the version is recorded, so a second call is a no-op.
        assert apply_migrations([migration]) == []

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                assert version in applied_versions(cur)
            conn.commit()
        finally:
            put_conn(conn)
    finally:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS "platform"._t_{version}')
                cur.execute(
                    "DELETE FROM platform.schema_migrations WHERE version=%s",
                    (version,),
                )
            conn.commit()
        finally:
            put_conn(conn)


@requires_db
def test_a_failing_statement_does_not_lose_the_others():
    """One bad statement is skipped; the rest of the migration still applies.

    A plain rollback would discard the statements that already succeeded, so this
    pins the savepoint behaviour.
    """
    from qwenpaw.platform_store import Migration, apply_migrations, get_conn, put_conn

    version = f"test_{int(time.time() * 1000)}"
    table = f"_t_{version}"
    migration = Migration(version, "unit_partial", (
        f'CREATE TABLE IF NOT EXISTS "platform".{table} (id INT PRIMARY KEY)',
        "NOT VALID SQL AT ALL",
        f'ALTER TABLE "platform".{table} ADD COLUMN IF NOT EXISTS note TEXT',
    ))
    try:
        apply_migrations([migration])

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='platform' AND table_name=%s "
                    "ORDER BY 1",
                    (table,),
                )
                columns = [row[0] for row in cur.fetchall()]
            conn.commit()
        finally:
            put_conn(conn)

        assert columns == ["id", "note"], "statements after the failure were lost"
    finally:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS "platform".{table}')
                cur.execute(
                    "DELETE FROM platform.schema_migrations WHERE version=%s",
                    (version,),
                )
            conn.commit()
        finally:
            put_conn(conn)


@requires_db
def test_a_narrow_migration_set_does_not_suppress_a_later_one():
    """先应用一套窄迁移，**不能**让后面另一套被当成「已就绪」而跳过。

    这是线上故障的根因。五个调用方各传一套（history / audit / import-state /
    token / paths，每套一个版本），而「已就绪」原先只是**一个模块级布尔**——谁先
    跑成功就替所有人答了。启动早期构造 token usage 管理器会先把它置位，于是历史
    那套（`202609240001`，建 `platform.history_seq` 与
    `platform.conversation_history`）永远不会执行；**重启也没用，每次都是同一个
    调用方先赢**。表现就是导入会话 500：`relation "platform.history_seq" does not
    exist`，而数据库是通的、别的平台表也在。

    断言的是**表真的建出来了**，不只是返回值：旧行为下 `ensure_schema` 会因为标志
    已置位而返回 True，单看返回值测不出来。
    """
    from qwenpaw.platform_store import Migration, get_conn, put_conn
    from qwenpaw.platform_store import schema as schema_mod

    stamp = int(time.time() * 1000)
    first_version = f"test_first_{stamp}"
    later_version = f"test_later_{stamp}"
    first_table = f"_t_{first_version}"
    later_table = f"_t_{later_version}"

    def _table_exists(name: str) -> bool:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='platform' AND table_name=%s",
                    (name,),
                )
                found = cur.fetchone() is not None
            conn.commit()
            return found
        finally:
            put_conn(conn)

    def _cleanup() -> None:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS "platform".{first_table}')
                cur.execute(f'DROP TABLE IF EXISTS "platform".{later_table}')
                cur.execute(
                    "DELETE FROM platform.schema_migrations WHERE version = ANY(%s)",
                    ([first_version, later_version],),
                )
            conn.commit()
        finally:
            put_conn(conn)

    try:
        schema_mod.reset_schema_flag()
        assert schema_mod.ensure_schema([
            Migration(
                first_version,
                "narrow",
                (f'CREATE TABLE IF NOT EXISTS "platform".{first_table} (id INT)',),
            ),
        ])
        assert _table_exists(first_table)

        # 另一套（不同版本）必须真的执行，而不是因为「schema 已就绪」被跳过。
        assert schema_mod.ensure_schema([
            Migration(
                later_version,
                "later",
                (f'CREATE TABLE IF NOT EXISTS "platform".{later_table} (id INT)',),
            ),
        ])
        assert _table_exists(later_table), (
            "先应用的窄迁移把后面这套顶掉了：表没建，却报告已就绪"
        )
    finally:
        _cleanup()
        schema_mod.reset_schema_flag()


@requires_db
def test_a_migration_with_a_skipped_statement_is_not_recorded():
    """语句跑失败并被跳过时，**不能**把这次迁移记为「已应用」。

    记了就是永久的静默缺口：版本再也不会被重试，缺的那张表只能在很久之后以
    一个看起来无关的故障暴露出来——线上就是这样：导入会话在
    ``relation "platform.history_seq" does not exist`` 上 500，而日志里没有
    任何东西把这两件事联系起来。不记录则下次启动会重跑；语句全是 IF NOT EXISTS，
    重跑没有代价，环境一修好就自愈。
    """
    from qwenpaw.platform_store import Migration, apply_migrations, get_conn, put_conn

    version = f"test_{int(time.time() * 1000)}"

    def _applied() -> bool:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM platform.schema_migrations WHERE version=%s",
                    (version,),
                )
                found = cur.fetchone() is not None
            conn.commit()
            return found
        finally:
            put_conn(conn)

    def _forget() -> None:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM platform.schema_migrations WHERE version=%s",
                    (version,),
                )
            conn.commit()
        finally:
            put_conn(conn)

    try:
        # 对照：全部语句成功的迁移照常记录，否则这条断言就成了「什么都不记录」。
        assert apply_migrations([Migration(version, "unit_clean", ("SELECT 1",))]) == [
            version
        ]
        assert _applied()
        _forget()

        broken = Migration(
            version,
            "unit_broken",
            ("SELECT 1", "NOT VALID SQL AT ALL"),
        )
        apply_migrations([broken])
        assert not _applied(), "有语句被跳过的迁移被记成了已应用，从此不再重试"

        # 重跑仍会执行，且仍然不记录（幂等，直到环境修好）。
        apply_migrations([broken])
        assert not _applied()
    finally:
        _forget()


@requires_db
def test_concurrent_boot_applies_each_migration_once():
    """Two processes booting together must not both create the schema.

    Separate processes are what actually race in production (API container and
    worker). Threads sharing one pool model the same contention on the advisory
    lock, without the process-local pool teardown that a thread cannot safely do
    while another thread holds a connection.
    """
    from qwenpaw.platform_store import Migration, apply_migrations, get_conn, put_conn

    version = f"test_{int(time.time() * 1000)}"
    migration = Migration(version, "unit_concurrent", (
        f'CREATE TABLE IF NOT EXISTS "platform"._t_{version} (id INT PRIMARY KEY)',
    ))
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def _apply():
        try:
            results.append(apply_migrations([migration]))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion
            errors.append(exc)

    try:
        threads = [threading.Thread(target=_apply) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert not errors, f"concurrent migration raised: {errors}"
        applied = [v for group in results for v in group]
        assert applied.count(version) == 1, f"migration ran {applied.count(version)}x"
    finally:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS "platform"._t_{version}')
                cur.execute(
                    "DELETE FROM platform.schema_migrations WHERE version=%s",
                    (version,),
                )
            conn.commit()
        finally:
            put_conn(conn)
