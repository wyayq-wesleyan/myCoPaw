# -*- coding: utf-8 -*-
"""恢复中断任务时必须尊重 attempts 上限，否则毒文档会让队列永久停摆。

线上形态：一个文档让 worker 进程崩溃（OOM / 原生库 segfault）。容器重启后，
`recover_interrupted_jobs` 把它重新排队；`claim_job` 按 `created_at` 取最旧，
于是它**永远排在最前**——每次重启都先领到它、再崩一次，其它上传全部卡在
「等待 Worker」，看起来像队列永久卡死。

`requeue_stale_jobs` 早就有「超过 attempts 就直接判失败」的分支，但
`recover_interrupted_jobs` 漏了。这里锁住两点：
1. 超过上限的任务在恢复时置为 failed，不再重排；
2. 未超限的任务照常重排（不能因为加了守卫就把正常任务也挡住）。

第二点在实现中踩过坑：最初用 `%s::int IS NULL OR attempts < %s`，PostgreSQL
把参数推断成 unknown 时比较结果与直觉不符，把正常任务一起挡掉了 —— 所以守卫
在 Python 侧拼 SQL 片段。这条测试因此也要断言「正常任务仍然被重排」。
"""
from __future__ import annotations

import importlib.util
import sys
import threading
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
STORE_PATH = REPO_ROOT / "knowledge-service" / "app" / "store.py"

try:
    import psycopg_pool  # noqa: F401

    from psycopg.rows import dict_row  # noqa: F401

    _HAS_PSYCOPG = True
except ImportError:  # pragma: no cover
    _HAS_PSYCOPG = False

import os

_DSN = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL", "").strip()

requires_db = pytest.mark.skipif(
    not (_DSN and _HAS_PSYCOPG),
    reason="set KNOWLEDGE_TEST_DATABASE_URL (with psycopg3) to run",
)


def _load_store():
    spec = importlib.util.spec_from_file_location("store_recovery_ut", STORE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 源码契约（永远执行）
# ---------------------------------------------------------------------------


def test_recovery_accepts_an_attempts_cap() -> None:
    src = STORE_PATH.read_text(encoding="utf-8")
    i = src.index("def recover_interrupted_jobs")
    body = src[i: src.index("def requeue_stale_jobs", i)]
    assert "max_attempts" in body, (
        "recover_interrupted_jobs 没有 attempts 上限：毒文档会被无限重启"
    )


def test_attempts_guard_is_not_a_dead_variable() -> None:
    """守卫必须真的产生 SQL 约束，而不是算出来没用。

    只断言「代码里有 `attempts_guard`」是测不中的：把守卫改成常量空串，
    那些字符串断言照样通过，而毒文档依旧会被无限重排。所以这里检查**计算
    出来的 SQL 片段本身**：给一个上限时必须含有比较，不给时必须为空。
    """
    src = STORE_PATH.read_text(encoding="utf-8")
    i = src.index("attempts_guard = (")
    j = src.index(")", src.index("else", i)) + 1
    snippet = src[i:j]

    def guard_for(cap):
        """在隔离命名空间里求值那段表达式（不导入 psycopg）。"""
        namespace = {"max_attempts": cap}
        exec(compile(snippet, "<guard>", "exec"), namespace)  # noqa: S102
        return namespace["attempts_guard"]

    assert guard_for(4) == "AND attempts < 4", (
        f"守卫没有生成限制条件，实际 {guard_for(4)!r}"
    )
    assert guard_for(None) == "", "不传上限时不应有限制"
    assert 'f"AND attempts < {int(max_attempts)}"' in snippet, (
        "守卫退化成了常量：毒文档仍会被无限重排"
    )
    # 不能再用被证明有问题的写法
    src = STORE_PATH.read_text(encoding="utf-8")
    i = src.index("def recover_interrupted_jobs")
    body = src[i: src.index("def requeue_stale_jobs", i)]
    assert "%s::int IS NULL OR attempts < %s" not in body, (
        "恢复成旧写法了：参数被推断成 unknown 时会把正常任务一起挡掉"
    )


# ---------------------------------------------------------------------------
# 真实数据库行为
# ---------------------------------------------------------------------------


@requires_db
class TestAgainstRealPostgres:
    @staticmethod
    def _store():
        module = _load_store()
        store = module.Store.__new__(module.Store)
        store.database_url = _DSN
        store._init_lock = threading.Lock()
        from psycopg_pool import ConnectionPool

        store._pool = ConnectionPool(
            _DSN, min_size=1, max_size=3, open=True,
            kwargs={"row_factory": dict_row}, name="recovery-ut",
        )
        return store

    def _seed(self, store) -> tuple[str, str, str, str]:
        suffix = uuid.uuid4().hex[:8]
        space_id = f"s-{suffix}"
        doc_id = f"d-{suffix}"
        # 两个任务挂在同一个文档上，一个重试 4 次（毒），一个 1 次（正常）
        poison, healthy = f"jp-{suffix}", f"jo-{suffix}"
        with store._connect() as db:
            db.execute(
                "INSERT INTO spaces (id,name,owner_id,created_at) VALUES (%s,'t',%s,now())",
                (space_id, f"o-{suffix}"),
            )
            db.execute(
                "INSERT INTO documents (id,space_id,owner_id,filename,path,status,"
                "created_at,updated_at) VALUES (%s,%s,%s,'x','/x','processing',now(),now())",
                (doc_id, space_id, f"o-{suffix}"),
            )
            for job_id, attempts in ((poison, 4), (healthy, 1)):
                db.execute(
                    "INSERT INTO jobs (id,document_id,status,attempts,created_at,"
                    "updated_at,owner_worker_id) "
                    "VALUES (%s,%s,'processing',%s,now(),now()-interval '2 hours',NULL)",
                    (job_id, doc_id, attempts),
                )
        return space_id, doc_id, poison, healthy

    def _cleanup(self, store, space_id, doc_id, *job_ids) -> None:
        with store._connect() as db:
            db.execute(
                "DELETE FROM jobs WHERE id = ANY(%s)", (list(job_ids),),
            )
            db.execute("DELETE FROM documents WHERE id=%s", (doc_id,))
            db.execute("DELETE FROM spaces WHERE id=%s", (space_id,))

    def _statuses(self, store, *job_ids) -> dict[str, str]:
        with store._connect() as db:
            rows = db.execute(
                "SELECT id, status FROM jobs WHERE id = ANY(%s)", (list(job_ids),),
            ).fetchall()
        return {row["id"]: row["status"] for row in rows}

    def test_poison_job_gives_up_while_healthy_one_requeues(self) -> None:
        store = self._store()
        space_id, doc_id, poison, healthy = self._seed(store)
        try:
            recovered = store.recover_interrupted_jobs(max_attempts=4)
            statuses = self._statuses(store, poison, healthy)

            assert statuses[poison] == "failed", (
                "超过 attempts 上限的任务仍被重排，会导致重启死循环"
            )
            assert statuses[healthy] == "queued", (
                "正常任务被守卫一并挡掉了；守卫只应拦住超限的那批"
            )
            assert recovered == 1, (
                f"返回值应只统计重新排队的任务，实际 {recovered}"
            )
        finally:
            self._cleanup(store, space_id, doc_id, poison, healthy)

    def test_without_a_cap_both_are_requeued(self) -> None:
        """不传 max_attempts 时保持旧行为，确认这次改动没有改变调用方语义。"""
        store = self._store()
        space_id, doc_id, poison, healthy = self._seed(store)
        try:
            recovered = store.recover_interrupted_jobs(max_attempts=None)
            statuses = self._statuses(store, poison, healthy)

            assert statuses[poison] == "queued"
            assert statuses[healthy] == "queued"
            assert recovered == 2
        finally:
            self._cleanup(store, space_id, doc_id, poison, healthy)
