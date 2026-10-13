# -*- coding: utf-8 -*-
"""语义检索必须走 pgvector，而不是把全部向量读回 Python。

背景：``search_by_embedding`` 原来的实现是「把整个 space 的 chunk 向量全量读回
Python，再逐个算余弦」。开销与内存都随语料线性增长，实测：

    chunks    耗时      内存增量
    20,000    11.1s     +1.24 GB
    40,000    23.5s     +2.49 GB
    60,000    34.6s     +3.74 GB
   150,000    OOM 崩溃   约 +9.3 GB

生产 ``KNOWLEDGE_API_MEMORY_LIMIT=16g``，所以「上传 2000 多份文档后对话变慢」
的真身是：每轮对话前的自动检索要几十秒，语料再大就把 knowledge-api 打到 OOM。

knowledge-service 有自己的依赖（psycopg3），不在主应用的虚拟环境里，所以这些测试
沿用同目录既有测试的做法：以源码 + 真实 PostgreSQL 来验证契约，而不是 import 模块。

  - 源码级断言（永远执行、不需要环境）
  - 数据库级断言（设置了 ``KNOWLEDGE_TEST_DATABASE_URL`` 才执行）
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
    """Return one method's source, up to the next top-level ``def``."""
    src = _store_source()
    match = re.search(rf"\n    def {re.escape(name)}\(", src)
    assert match, f"{name} not found in store.py"
    start = match.start()
    nxt = re.search(r"\n    (?:def|@)", src[start + 1:])
    end = start + 1 + nxt.start() if nxt else len(src)
    return src[start:end]


# ---------------------------------------------------------------------------
# 源码级契约
# ---------------------------------------------------------------------------


class TestSearchGoesThroughTheDatabase:
    def test_neighbour_search_uses_pgvector_operator(self) -> None:
        """快路径必须用 ``<=>`` 让数据库做近邻检索。"""
        body = _method_body("_vector_search_sql")
        assert "<=>" in body, (
            "the fast path must use the pgvector distance operator, otherwise "
            "PostgreSQL cannot use the HNSW index"
        )
        assert "ORDER BY ce.embedding <=>" in body, (
            "ordering must happen in the database so the index is used"
        )
        assert "LIMIT" in body

    def test_fast_path_does_not_read_jsonb_vectors(self) -> None:
        """关键契约：快路径不得选 ``vector_json`` —— 那正是 OOM 的来源。"""
        body = _method_body("_vector_search_sql")
        assert "vector_json" not in body, (
            "pulling the jsonb vectors back into Python is exactly what caused "
            "the OOM; the fast path must select only the pgvector column"
        )

    def test_dispatch_prefers_the_index_then_falls_back(self) -> None:
        body = _method_body("search_by_embedding")
        assert "_has_vector_column" in body
        assert "_vector_search_sql" in body
        assert "_score_in_python" in body, (
            "an un-backfilled database must still retrieve through the old path"
        )

    def test_fallback_reads_jsonb_explicitly(self) -> None:
        """退回路径才是唯一允许读 jsonb 向量的地方。"""
        body = _method_body("_load_all_vectors")
        assert "vector_json" in body
        assert "ce.vector_json" in body

    def test_empty_vector_short_circuits(self) -> None:
        body = _method_body("search_by_embedding")
        assert "if not vector:" in body


class TestBackfillContract:
    def test_conversion_happens_inside_the_update(self) -> None:
        """转换必须由 PostgreSQL 做，不能把向量文本搬过网络。"""
        body = _method_body("backfill_embedding_vectors")
        assert "(b.vector_json::text)::vector" in body, (
            "the cast must happen inside the UPDATE; a Python round trip costs an "
            "extra full copy of every vector"
        )

    def test_scan_filters_by_declared_width(self) -> None:
        """维度不符的行必须被排除，否则回填会永远卡在同一批。"""
        body = _method_body("backfill_embedding_vectors")
        assert "jsonb_array_length(vector_json) = %s" in body

    def test_only_converts_rows_still_null(self) -> None:
        """幂等：已转换的行不会重复处理，中断后可续跑。"""
        body = _method_body("backfill_embedding_vectors")
        assert "embedding IS NULL" in body
        assert "ce.embedding IS NULL" in body

    def test_stops_when_a_batch_converts_nothing(self) -> None:
        body = _method_body("backfill_embedding_vectors")
        assert "if batch_done == 0" in body, (
            "a batch that converts nothing must end the loop instead of spinning"
        )

    def test_batches_are_bounded(self) -> None:
        assert "VECTOR_BACKFILL_BATCH" in _store_source()
        body = _method_body("backfill_embedding_vectors")
        assert "LIMIT %s" in body


class TestIndexIsDeferredUntilBackfillCompletes:
    """建索引必须等回填完成 —— 边回填边维护索引要慢 55 倍。"""

    def test_startup_does_not_index_while_rows_are_pending(self) -> None:
        src = _store_source()
        init = src[src.find("def _init_db("):src.find("def _migrate(")]
        assert "_count_unconverted" in init, (
            "startup must check whether a backfill is still owed before creating "
            "the HNSW index; maintaining it row-by-row during the backfill "
            "measured 50 rows/s versus 2759 rows/s without it"
        )

    def test_index_creation_waits_for_a_drained_column(self) -> None:
        body = _method_body("rebuild_vector_index_if_needed")
        assert "_count_unconverted" in body
        assert "if self._count_unconverted" in body.replace("return False", "")

    def test_index_creation_is_best_effort(self) -> None:
        body = _method_body("rebuild_vector_index_if_needed")
        assert "except Exception" in body, (
            "a failure to build the index must not break retrieval"
        )

    def test_write_path_does_not_require_the_vector_column(self) -> None:
        """扩展装不上时，写入必须仍然成功（只写 jsonb）。"""
        src = _store_source()
        assert "_vector_column_available" in src
        idx = src.find("vector_ready = self._vector_column_available()")
        assert idx > 0, "the save path must gate the vector write on availability"
        region = src[idx:idx + 2000]
        assert "else:" in region, (
            "there must be a jsonb-only branch for databases without the column"
        )
        assert "INSERT INTO chunk_embeddings (chunk_id, vector_json)" in region


class TestVectorLiteral:
    def test_helper_uses_shortest_float_form(self) -> None:
        src = _store_source()
        start = src.find("def _vector_literal(")
        assert start > 0, "_vector_literal helper is missing"
        body = src[start:start + 900]
        assert ":.8g" in body, (
            "full f64 repr balloons a 1024-dim vector to ~22k characters; "
            "'.8g' keeps it compact and still well beyond float32 precision"
        )


# ---------------------------------------------------------------------------
# 真实数据库契约（可选：设置 KNOWLEDGE_TEST_DATABASE_URL 后执行）
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
class TestAgainstRealPostgres:
    """在真实库上验证：索引被使用、内存不随语料增长、结果不回退。"""

    @staticmethod
    def _store():
        import sys

        sys.path.insert(0, str(REPO_ROOT / "knowledge-service"))
        from app.store import Store  # noqa: PLC0415

        return Store(_DSN)

    def test_neighbour_query_uses_the_hnsw_index(self) -> None:
        """查询计划必须出现 HNSW 索引 —— 否则仍是一次全表扫描。"""
        import psycopg  # noqa: PLC0415

        store = self._store()
        if not store._vector_column_available():
            pytest.skip("pgvector column not present")
        vec = "[" + ",".join(["0.01"] * 1024) + "]"
        with psycopg.connect(_DSN) as db:
            plan = "\n".join(
                r["QUERY PLAN"] for r in db.execute(
                    """
                    EXPLAIN SELECT c.id FROM chunk_embeddings ce
                    JOIN chunks c ON c.id = ce.chunk_id
                    WHERE ce.embedding IS NOT NULL
                    ORDER BY ce.embedding <=> %s::vector LIMIT 20
                    """,
                    (vec,),
                ).fetchall()
            )
        assert "idx_chunk_embeddings_hnsw" in plan, (
            "the planner did not use the HNSW index:\n" + plan
        )

    def test_search_returns_rows_and_respects_min_score(self) -> None:
        store = self._store()
        with store._connect() as db:
            row = db.execute(
                "SELECT space_id FROM chunks LIMIT 1",
            ).fetchone()
        if not row:
            pytest.skip("no chunks in the test database")
        space_id = row["space_id"]

        results = store.search_by_embedding(space_id, [0.01] * 1024, limit=5, min_score=0.0)
        assert results, "search should return candidates for an arbitrary vector"

        filtered = store.search_by_embedding(
            space_id, [0.01] * 1024, limit=5, min_score=0.999,
        )
        assert all(item["score"] >= 0.999 for item in filtered)

    def test_search_does_not_load_the_whole_space(self) -> None:
        """内存契约：检索占用的内存不应随 space 的 chunk 数线性增长。

        用 Python 侧的对象增量粗略量化：全量读回时，一个 6 万 chunk 的 space 会
        多出数十万个对象；走索引时只有 limit 条。
        """
        import tracemalloc  # noqa: PLC0415

        store = self._store()
        with store._connect() as db:
            row = db.execute(
                "SELECT space_id, count(*) AS n FROM chunks "
                "GROUP BY space_id ORDER BY n DESC LIMIT 1",
            ).fetchone()
        if not row or row["n"] < 1000:
            pytest.skip("need a space with >=1000 chunks to detect a full scan")

        tracemalloc.start()
        try:
            store.search_by_embedding(row["space_id"], [0.01] * 1024, limit=20)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        # 全量读回 1000 个 1024 维向量至少要几 MB；索引路径只取 20 条。
        assert peak < 4 * 1024 * 1024, (
            f"peak allocation {peak/1e6:.1f} MB suggests the whole space was "
            f"loaded instead of using the index"
        )


# ---------------------------------------------------------------------------
# 启动期不得构建索引（否则知识服务迟迟不能响应 → 控制台 503）
# ---------------------------------------------------------------------------


class TestStartupNeverBuildsTheIndex:
    """``Store.__init__`` 是**导入期**执行的，它里面不能做耗时工作。

    ``main.py`` 在模块顶层构造 ``Store``，即 uvicorn 能应答任何请求之前。
    在该函数里建 HNSW 索引会让启动阻塞整个构建时长 —— 实测 5 万行 22.4 秒，
    生产规模是分钟级。表现就是控制台探测超时、知识空间页报 503，直到建完。

    索引构建放在 worker 的空闲分支（那里慢不影响任何人）。
    """

    @staticmethod
    def _init_db_body() -> str:
        """The body of ``_init_db`` only, not helpers defined after it.

        Slicing up to ``_migrate`` used to be enough, but the pgvector helpers are
        now defined between the two, so that slice pulled in
        ``_create_hnsw_index_statement`` itself and the assertion could never pass.
        """
        src = _store_source()
        start = src.find("def _init_db(")
        assert start > 0
        rest = src[start:]
        nxt = rest.find("\n    def ", 10)
        return rest[: nxt if nxt > 0 else len(rest)]

    def test_init_db_does_not_create_the_index(self) -> None:
        body = self._init_db_body()
        # Comments may mention the index (explaining why it is not built here);
        # an actual call is what must not appear.
        code = "\n".join(
            line for line in body.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "_create_hnsw_index_statement(" not in code, (
            "启动路径仍在调用建索引：会把知识服务的可用时间推迟到索引建完"
        )

    def test_count_helper_still_consulted_for_logging(self) -> None:
        """不建索引，但仍要能看出还有多少待转换（便于运维判断进度）。"""
        assert "_count_unconverted" in self._init_db_body()

    def test_index_build_is_left_to_the_worker(self) -> None:
        worker = (
            Path(__file__).resolve().parents[3]
            / "knowledge-service" / "app" / "worker.py"
        ).read_text(encoding="utf-8")
        assert "rebuild_vector_index_if_needed" in worker, (
            "索引构建没有后台承接方：启动不建、后台也不建，则永远没有索引"
        )


class TestWorkerBuildsTheIndexEvenWithNothingToConvert:
    """已回填完但缺索引的库，必须也能补建。

    升级过程中「回填完成」和「索引建立」是两个时刻。若索引构建被放在
    ``if converted:`` 里面，那么一个 conversion 数为 0 的库（回填早已完成、
    或上一轮刚好把最后一批转完）就永远不会被建索引 —— 语义检索一直走慢路径，
    这正是升级后 503 的那批部署所处的状态。

    这里用行为验证而不是读源码缩进：缩进检查无法区分
    ``if converted:`` 与并列语句（两者缩进相同），所以必须真的跑一遍。
    """

    @staticmethod
    def _load_worker():
        """Import ``app.worker`` with the knowledge-service package on sys.path."""
        import sys

        root = Path(__file__).resolve().parents[3] / "knowledge-service"
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import app.worker as worker  # noqa: PLC0415

        return worker

    def test_builds_the_index_when_there_was_nothing_to_convert(self) -> None:
        try:
            worker = self._load_worker()
        except Exception as exc:  # noqa: BLE001 - 依赖不在时跳过
            pytest.skip(f"knowledge-service 依赖不可用: {exc}")

        class _Store:
            """已回填完（0 待转换）、且索引尚未建立的库。"""

            def __init__(self) -> None:
                self.index_built = 0

            def backfill_embedding_vectors_until_done(self) -> int:
                return 0  # 没有要转换的

            def count_unconverted_embeddings(self) -> int:
                return 0

            def rebuild_vector_index_if_needed(self) -> bool:
                self.index_built += 1
                return True

        store = _Store()
        # 清掉 latch，确保这次调用真的执行（而不是被「已完成」短路）
        worker._vector_backfill_state["done"] = False
        worker._vector_backfill_state["last_at"] = 0.0
        try:
            worker._backfill_vectors_while_idle(store)
        finally:
            worker._vector_backfill_state["done"] = False
            worker._vector_backfill_state["last_at"] = 0.0

        assert store.index_built == 1, (
            "没有待转换的行时没有建立索引：这类库（回填早已完成）会永远缺索引"
        )

    def test_latch_is_not_set_when_the_build_fails(self) -> None:
        """建索引失败时不能上锁，否则从此不再重试。"""
        try:
            worker = self._load_worker()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"knowledge-service 依赖不可用: {exc}")

        class _Store:
            def backfill_embedding_vectors_until_done(self) -> int:
                return 0

            def count_unconverted_embeddings(self) -> int:
                return 0

            def rebuild_vector_index_if_needed(self) -> bool:
                return False  # 建失败

        worker._vector_backfill_state["done"] = False
        worker._vector_backfill_state["last_at"] = 0.0
        try:
            worker._backfill_vectors_while_idle(_Store())
            done = worker._vector_backfill_state["done"]
        finally:
            worker._vector_backfill_state["done"] = False
            worker._vector_backfill_state["last_at"] = 0.0

        assert done is False, (
            "建索引失败却把迁移标记为完成：之后不会再重试，索引永远缺失"
        )
