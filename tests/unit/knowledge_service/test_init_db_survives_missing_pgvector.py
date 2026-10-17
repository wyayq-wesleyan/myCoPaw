# -*- coding: utf-8 -*-
"""``_init_db`` 必须在没有 pgvector 的数据库上把服务拉起来，而不是崩掉。

线上故障（2026-10-13）：服务器上的 PostgreSQL 是 ``postgres:17-alpine``，
不含 pgvector。新版 ``_init_db`` 里有一句量列宽的探测：

    SELECT vector_dims((vector_json::text)::vector) ...

在没有 ``vector`` 类型的库上它报 ``UndefinedObject``。这句探测**没有 savepoint**，
于是整个事务被标记为 aborted —— 之后每一条语句都失败，**包括 ``_migrate`` 用
savepoint 包起来的那批**（savepoint 救不回已经作废的外层事务），最后
``_connect()`` 归还连接时 ``commit()`` 抛出：

    psycopg.ProgrammingError: Explicit commit() forbidden within a Transaction context.

``Store.__init__`` 抛出 → ``main.py`` 在**模块导入期**构造 ``Store`` → uvicorn 起不来
→ 容器反复重启 → 主应用每个代理请求都拿到
``Server disconnected without sending a response``，知识空间整页报 503。

也就是说：一个「只是缺扩展」的库，被降级成**整体不可用**。代码里原本写着
「任何一步失败都只降级到 jsonb 全量比对，不影响可用性」，这个承诺当时是假的。

两处独立充分的修复（各自单独就够，互为保险）：
  1. ``_vector_type_available`` 前置能力检测，缺扩展就不进 pgvector 分支；
  2. 探测语句用 savepoint 包住，失败也不会污染外层事务。

知识服务自带依赖（psycopg3），主应用 venv 里没有，所以这里沿用同目录既有做法：
源码级契约（永远执行）+ 真实库行为（设了 ``KNOWLEDGE_TEST_DATABASE_URL`` 才执行）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
STORE_PATH = REPO_ROOT / "knowledge-service" / "app" / "store.py"


def _store_source() -> str:
    return STORE_PATH.read_text(encoding="utf-8")


def _method_body(name: str) -> str:
    """One method's source, up to the next top-level def/decorator."""
    src = _store_source()
    match = re.search(rf"\n    def {re.escape(name)}\(", src)
    assert match, f"{name} not found in store.py"
    start = match.start()
    nxt = re.search(r"\n    (?:def|@)", src[start + 1:])
    end = start + 1 + nxt.start() if nxt else len(src)
    return src[start:end]


def _init_db_body() -> str:
    """The body of ``_init_db`` only — helpers are defined after it."""
    src = _store_source()
    start = src.find("def _init_db(")
    assert start > 0
    rest = src[start:]
    nxt = rest.find("\n    def ", 10)
    return rest[: nxt if nxt > 0 else len(rest)]


def _strip_comments(body: str) -> str:
    return "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )


def _code_only(body: str) -> str:
    """Method source with comments **and the docstring** removed.

    The docstrings here deliberately name the very things the tests assert on
    ("asked with ``to_regtype`` rather than by attempting a cast"), so matching
    raw source passes even after the code is reverted — a test that reads the
    explanation instead of the behaviour.
    """
    return re.sub(r'"""[\s\S]*?"""', "", _strip_comments(body))


# ---------------------------------------------------------------------------
# 源码级契约（永远执行）
# ---------------------------------------------------------------------------


class TestNoBareStatementRunsInTheInitTransaction:
    """不变量：``_init_db`` 的事务里不能出现**没有 savepoint 的裸语句**。

    一个裸语句失败就会作废整个事务，而 savepoint 救不回已作废的外层事务 ——
    这正是本文件开头描述的那次故障的机制。所以「建表语句也必须走 ``_migrate``」
    不是风格问题，是这条不变量的一部分。
    """

    def test_every_statement_goes_through_migrate(self) -> None:
        code = _strip_comments(_init_db_body())
        assert not re.search(r"for statement in statements", code), (
            "建表语句又变回裸循环了：其中任何一条失败都会作废整个初始化事务，"
            "导致后续迁移全部失败、服务起不来"
        )
        assert re.search(r"self\._migrate\(\s*db,\s*statements\s*\)", code), (
            "建表语句没有走 _migrate：失去 savepoint 隔离"
        )

    def test_the_advisory_lock_is_still_taken(self) -> None:
        """API 与 worker 同时启动，首次建表必须串行。"""
        assert "pg_advisory_xact_lock" in _init_db_body()


class TestTheVectorProbeCannotPoisonTheTransaction:
    def test_init_db_checks_capability_before_probing(self) -> None:
        """缺扩展的库要在探测**之前**就被识别出来。"""
        body = _strip_comments(_init_db_body())
        assert "_vector_type_available" in body, (
            "缺少 pgvector 能力前置检测：无 pgvector 的库会走进探测路径"
        )
        gate = body.index("_vector_type_available")
        probe = body.index("_resolve_embedding_dimensions")
        assert gate < probe, (
            "能力检测必须在量列宽之前，否则探测已经先把事务打死了"
        )

    def test_the_gate_returns_early_with_a_readable_reason(self) -> None:
        body = _code_only(_init_db_body())
        segment = body[body.index("_vector_type_available"):]
        assert "logger.warning(" in segment, (
            "缺 pgvector 时必须留下 WARNING：否则运维只看到语义检索变慢，无从解释"
        )
        assert "jsonb fallback" in segment, (
            "告警要说明退化到哪里，而不是只说一句「不可用」"
        )
        assert re.search(r"return\s*$", segment.split("logger.warning(")[1], re.M), (
            "检测到缺扩展后必须直接跳过 pgvector 分支，不能继续往下探测"
        )

    def test_dimension_probe_is_wrapped_in_a_savepoint(self) -> None:
        body = _method_body("_resolve_embedding_dimensions")
        probe_at = body.index("vector_dims")
        try_at = body.rindex("try:", 0, probe_at)
        segment = body[try_at:probe_at]
        assert "with db.transaction():" in segment, (
            "量列宽的探测没有 savepoint：它在无 pgvector 库上会报 UndefinedObject，"
            "从而作废外层事务，把「缺扩展」变成「服务起不来」"
        )

    def test_dimension_probe_failure_is_visible(self) -> None:
        """告警必须挂在**探测**上，而不是方法里别的 logger.warning。

        方法末尾还有一条 EMBEDDING_DIMENSIONS 不一致的 warning，所以断言
        「方法里有 warning」是测不中的 —— 必须认准这一条的具体文案。
        """
        body = _code_only(_method_body("_resolve_embedding_dimensions"))
        assert (
            'logger.warning("pgvector: could not measure existing dimensions'
            in body
        ), (
            "量不到列宽时只记 debug/别的分支：生产日志级别是 info，这个信号永远不会出现"
        )

    def test_capability_check_never_raises_through_a_cast(self) -> None:
        """用 ``to_regtype`` 问类型是否存在，而不是靠转换失败来试。"""
        body = _code_only(_method_body("_vector_type_available"))
        assert "to_regtype" in body, (
            "能力检测不要用「试着转换」的方式：转换失败会作废事务"
        )
        assert "::vector" not in body, (
            "能力检测里出现了 vector 转换：没有该类型时它会报错并作废事务"
        )
        assert "with db.transaction():" in body


# ---------------------------------------------------------------------------
# 真实数据库行为（可选：设置 KNOWLEDGE_TEST_DATABASE_URL 后执行）
# ---------------------------------------------------------------------------

_DSN = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL", "").strip()

try:  # only present in the knowledge-service environment
    import psycopg  # noqa: F401

    _HAS_PSYCOPG = True
except ImportError:  # pragma: no cover - the default test env
    _HAS_PSYCOPG = False

requires_db = pytest.mark.skipif(
    not _DSN or not _HAS_PSYCOPG,
    reason=(
        "set KNOWLEDGE_TEST_DATABASE_URL (and run where psycopg3 is installed, "
        "e.g. the knowledge-service container) to run database-level checks"
    ),
)


@requires_db
class TestMigrateIsolationOnRealPostgres:
    """在真实库上锁住 savepoint 隔离这条不变量本身。"""

    @staticmethod
    def _store_module():
        import sys

        sys.path.insert(0, str(REPO_ROOT / "knowledge-service"))
        from app import store as store_module  # noqa: PLC0415

        return store_module

    def test_a_failed_migration_leaves_the_transaction_usable(self) -> None:
        """一条迁移失败后，同一事务里的后续语句仍必须能执行。

        对照实验（同一连接上另跑一次）证明：**裸**语句失败会作废事务。
        """
        import psycopg  # noqa: PLC0415

        store = self._store_module()
        # Plain connection: positional rows. Do not depend on the store's
        # dict_row factory here -- the point is the transaction's state, not the
        # row shape, and a dict-style index on a tuple fails only at runtime.
        with psycopg.connect(_DSN) as db:
            db.execute("SELECT pg_advisory_xact_lock(2026082202)")
            store.Store._migrate(
                db,
                ["CREATE EXTENSION IF NOT EXISTS definitely_not_a_real_ext_xyz"],
            )
            # 事务必须还是干净的，否则「缺扩展」就会升级成「服务起不来」
            assert db.execute("SELECT 1").fetchone()[0] == 1
            # 而且后续迁移仍然生效
            store.Store._migrate(
                db,
                ["CREATE TABLE IF NOT EXISTS _migrate_isolation_probe (id INT)"],
            )
            assert db.execute(
                "SELECT to_regclass('public._migrate_isolation_probe')"
            ).fetchone()[0] is not None
            db.execute("DROP TABLE IF EXISTS _migrate_isolation_probe")
