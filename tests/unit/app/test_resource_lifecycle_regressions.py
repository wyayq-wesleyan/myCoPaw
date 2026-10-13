# -*- coding: utf-8 -*-
"""同类缺陷的回归测试：获取/标记不对称，以及无限增长的进程级状态。

这组测试针对的是与「连接池耗尽」同源的一类 bug：

    共享状态先被标记为「已占用 / 已完成 / 已禁用」，随后某个可能失败的操作
    抛错，标记却没有回滚 —— 于是系统进入一个**永久错误**的状态，重启才能恢复。

独立地看每一处都「只是个小疏漏」，但它们有共同的用户可见形态：用着用着就不行
了，而且不会自己好。
"""
from __future__ import annotations

import asyncio
import time
import uuid

import pytest


# ---------------------------------------------------------------------------
# 1. 导入任务调度：无事件循环时的标记必须回滚
# ---------------------------------------------------------------------------


class TestImportSchedulingDoesNotPoisonItsMark:
    """``_schedule_job`` 在无法创建任务时必须撤销「已调度」标记。

    启动恢复走 ``asyncio.to_thread(recover_import_jobs)``，工作线程里没有运行中的
    事件循环，``create_task`` 直接抛 ``RuntimeError``。旧实现先把 id 记入
    ``_scheduled_jobs`` 再创建任务，指望任务的 ``finally`` 来清除；抛错后 id 永久
    留在集合里。60 秒后的恢复循环每轮都被 ``already scheduled`` 挡掉，
    ``scheduled`` 计数却照加，于是任务永远停在 ``pending``，重启也无法自愈。
    """

    def test_mark_is_rolled_back_when_there_is_no_running_loop(self) -> None:
        from qwenpaw.data_tables import importer

        job_id = str(uuid.uuid4())
        importer._scheduled_jobs.discard(job_id)
        try:
            # 无线程内事件循环 —— 与启动恢复的执行环境一致。
            created = importer._schedule_job(job_id, "t1", "/tmp/x.csv", "x.csv")

            assert created is False, "无法创建任务时应如实返回 False"
            assert job_id not in importer._scheduled_jobs, (
                "标记未回滚：该任务会被永久跳过，重启也无法恢复"
            )
        finally:
            importer._scheduled_jobs.discard(job_id)

    def test_a_later_attempt_from_the_loop_still_schedules(self) -> None:
        """同一 id 在事件循环里必须能被正常调度 —— 这才是「不会永久卡住」。"""
        from qwenpaw.data_tables import importer

        job_id = str(uuid.uuid4())
        started: list[str] = []

        async def _fake_run(jid, *_args):
            started.append(jid)

        original = importer._run_import_job_bounded
        importer._run_import_job_bounded = _fake_run
        try:
            # 先在没有事件循环的线程里试一次（模拟启动恢复）
            assert importer._schedule_job(job_id, "t", "/tmp/x.csv", "x") is False

            async def once() -> None:
                assert importer._schedule_job(job_id, "t", "/tmp/x.csv", "x") is True
                await asyncio.sleep(0.05)

            asyncio.run(once())
            assert started == [job_id], "第二次尝试仍未调度该任务"
        finally:
            importer._run_import_job_bounded = original
            importer._scheduled_jobs.discard(job_id)

    def test_already_scheduled_reports_true(self) -> None:
        """已在调度中视为「已处理」，避免重复起任务。"""
        from qwenpaw.data_tables import importer

        job_id = str(uuid.uuid4())
        importer._scheduled_jobs.add(job_id)
        try:
            assert importer._schedule_job(job_id, "t", "/tmp/x.csv", "x") is True
        finally:
            importer._scheduled_jobs.discard(job_id)


class TestRecoveryReportsRealProgress:
    """恢复扫描必须只统计**真正起了任务**的行。

    旧实现无条件自增计数，即使 ``_schedule_job`` 静默失败也报「已调度 N 个」，
    日志看起来正常、实际什么都没做 —— 这是排查时最难的一点。
    """

    def test_count_reflects_only_successful_schedules(self, monkeypatch, tmp_path) -> None:
        from qwenpaw.data_tables import importer

        staged = tmp_path / "d.csv"
        staged.write_text("a\n1\n")

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def execute(self, *_a, **_k):
                return None

            def fetchall(self):
                return [
                    {"id": "j1", "table_id": "t", "file_path": str(staged), "filename": "d.csv"},
                    {"id": "j2", "table_id": "t", "file_path": str(staged), "filename": "d.csv"},
                ]

        class _Conn:
            def cursor(self, **_kw):
                return _Cur()

            def commit(self):
                return None

        monkeypatch.setattr(importer, "_get_conn", lambda: _Conn())
        monkeypatch.setattr(importer, "_put_conn", lambda _c: None)
        # 两个都调度失败（模拟无线程内事件循环）
        monkeypatch.setattr(importer, "_schedule_job", lambda *a, **k: False)

        assert importer.recover_import_jobs() == 0, (
            "调度失败却计入进度，日志会谎报成功"
        )


# ---------------------------------------------------------------------------
# 2. pgvector 可用性：否定结论不得被永久缓存
# ---------------------------------------------------------------------------


class TestVectorColumnAvailabilityIsNotLatched:
    """瞬时数据库错误不能让 pgvector 永久关闭。

    ``Store`` 是进程级单例，旧实现把探测结果（含异常导致的 ``False``）缓存下来。
    一次抖动就会让向量写入永久退回 jsonb：回填不再转换、HNSW 索引永不建立、
    语义检索一直走慢路径 —— 只有重启才恢复。
    """

    @staticmethod
    def _source() -> str:
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        return (root / "knowledge-service" / "app" / "store.py").read_text(
            encoding="utf-8",
        )

    def test_negative_result_is_not_cached(self) -> None:
        src = self._source()
        start = src.find("def _vector_column_available")
        assert start > 0
        body = src[start:start + 2200]

        # 只有肯定结论才落缓存。
        assert "if available:" in body, (
            "探测失败的结果被缓存了：一次瞬时错误会永久关闭向量写入"
        )
        assert "self._vector_col_cache = True" in body
        assert "self._vector_col_cache = available" not in body, (
            "仍在缓存可能的 False —— 否定结论必须是「下次重试」"
        )

    def test_cached_true_still_short_circuits(self) -> None:
        """肯定结论仍要生效，否则每次写文档都多一次查询。"""
        body = self._source()
        start = body.find("def _vector_column_available")
        assert "if cached:" in body[start:start + 2200]


# ---------------------------------------------------------------------------
# 3. 后台任务表：必须有淘汰，否则长期运行只增不减
# ---------------------------------------------------------------------------


class TestBackgroundTaskStoreIsBounded:
    """``_bg_tasks`` 保留整个模型输出，必须有 TTL + 上限。

    旧实现只写入从不删除，而 ``bg.result`` 装着该轮完整回答 —— 每次提交后台对话
    都留一份在内存里，直到进程结束。
    """

    def test_finished_tasks_expire(self, monkeypatch) -> None:
        from qwenpaw.app.routers import console

        with_tasks = console._bg_tasks
        with_tasks.clear()
        try:
            old = console._BackgroundTask(status="completed")
            old.finished_at = time.time() - console._BG_TASK_TTL_SECONDS - 10
            fresh = console._BackgroundTask(status="completed")
            fresh.finished_at = time.time()
            with_tasks["old"] = old
            with_tasks["fresh"] = fresh

            console._prune_bg_tasks_locked()

            assert "old" not in with_tasks, "过期的已完成任务没有被回收"
            assert "fresh" in with_tasks, "仍在保留窗口内的任务被误删"
        finally:
            with_tasks.clear()

    def test_running_tasks_are_never_evicted(self) -> None:
        """运行中的任务不能因为腾内存而被清掉 —— 它还要被轮询。"""
        from qwenpaw.app.routers import console

        with_tasks = console._bg_tasks
        with_tasks.clear()
        try:
            running = console._BackgroundTask(status="running", finished_at=None)
            with_tasks["running"] = running

            console._prune_bg_tasks_locked()

            assert "running" in with_tasks
        finally:
            with_tasks.clear()

    def test_cap_evicts_oldest_finished_first(self) -> None:
        from qwenpaw.app.routers import console

        with_tasks = console._bg_tasks
        with_tasks.clear()
        try:
            now = time.time()
            total = console._MAX_BG_TASKS + 25
            for i in range(total):
                bg = console._BackgroundTask(status="completed")
                # 序号越小越早完成
                bg.finished_at = now - (total - i)
                with_tasks[f"t{i}"] = bg

            console._prune_bg_tasks_locked()

            assert len(with_tasks) <= console._MAX_BG_TASKS
            # 保留的应是最新的那批
            assert "t0" not in with_tasks, "最早完成的任务未被淘汰"
            assert f"t{total - 1}" in with_tasks, "最新的任务被误删"
        finally:
            with_tasks.clear()


# ---------------------------------------------------------------------------
# 4. 登录限流的键空间：攻击者可控的键必须被回收
# ---------------------------------------------------------------------------


class TestLoginLimiterKeySpaceIsBounded:
    """IP / 用户名的键必须随窗口过期而回收。

    两个 map 都是按**攻击者可控**的值建键的 ``defaultdict``，而
    ``_cleanup_old_attempts`` 只裁剪条目内的 deque，键永不删除 ——
    未认证流量可以无限撑大内存。
    """

    def test_expired_keys_are_pruned(self) -> None:
        from qwenpaw.app.rate_limiter import LoginRateLimiter

        rl = LoginRateLimiter()
        for i in range(300):
            rl.record_login_attempt(f"10.0.0.{i % 256}", f"user{i}", success=False)
        assert len(rl.user_attempts) >= 300

        # 把时间推进到窗口之外，再触发一次记录以运行清理
        stale = time.time() - 120
        for store in (rl.ip_attempts, rl.user_attempts):
            for key, dq in list(store.items()):
                store[key] = type(dq)(
                    [(stale, s, *rest) for _ts, s, *rest in dq], maxlen=dq.maxlen,
                )
        rl._last_prune_at = 0.0
        rl.record_login_attempt("1.1.1.1", "current", success=False)

        assert len(rl.user_attempts) < 50, (
            f"键空间未回收：仍有 {len(rl.user_attempts)} 个条目"
        )
        assert len(rl.ip_attempts) < 50

    def test_pruning_does_not_weaken_account_lockout(self) -> None:
        """省内存不能削弱防护：连续失败仍要锁账号。"""
        from qwenpaw.app.rate_limiter import LoginRateLimiter

        rl = LoginRateLimiter()
        for _ in range(5):
            rl.record_login_attempt("1.2.3.4", "victim", success=False)
        assert rl.is_user_locked("victim"), "账号锁定失效了"

    def test_pruning_does_not_weaken_ip_limiting(self) -> None:
        from qwenpaw.app.rate_limiter import LoginRateLimiter

        rl = LoginRateLimiter()
        for i in range(60):
            rl.record_login_attempt("9.9.9.9", f"u{i}", success=False)
        assert rl.is_ip_rate_limited("9.9.9.9"), "IP 限流失效了"

    def test_active_lock_survives_a_prune_pass(self) -> None:
        """活跃锁不能被清理掉，否则锁定会被静默解除。"""
        from qwenpaw.app.rate_limiter import LoginRateLimiter

        rl = LoginRateLimiter()
        for _ in range(5):
            rl.record_login_attempt("5.5.5.5", "locked_guy", success=False)
        assert rl.is_user_locked("locked_guy")

        rl._last_prune_at = 0.0
        rl.record_login_attempt("6.6.6.6", "unrelated", success=False)

        assert rl.is_user_locked("locked_guy"), "清理误删了活跃锁"

    def test_successful_login_still_clears(self) -> None:
        from qwenpaw.app.rate_limiter import LoginRateLimiter

        rl = LoginRateLimiter()
        for _ in range(5):
            rl.record_login_attempt("1.2.3.4", "victim", success=False)
        rl.record_login_attempt("1.2.3.4", "victim", success=True)
        assert not rl.is_user_locked("victim")


# ---------------------------------------------------------------------------
# 5. LSP 客户端：崩溃的语言服务器必须被替换，而不是被复用
# ---------------------------------------------------------------------------


class TestLspClientRespawnsADeadServer:
    """死了的子进程必须重新拉起。

    ``_read_loop`` 在进程退出时清空等待者，但清不掉 ``_proc``。旧实现只看
    ``_proc is not None`` 就直接 return，于是崩溃后的客户端被池子一直发出去，
    该 (项目, 语言) 的所有 LSP 调用都失败 —— 只有重启进程或恰好被 LRU 淘汰才恢复。
    """

    def test_a_crashed_server_is_replaced(self, tmp_path) -> None:
        import sys

        from qwenpaw.agents.tools import _lsp_client as L

        class _NoHandshake(L.LspClient):
            """Skip the real JSON-RPC handshake; only the process lifecycle matters."""

            def _initialize(self) -> None:
                return None

        argv = [sys.executable, "-c", "import time; time.sleep(600)"]
        client = _NoHandshake(argv, tmp_path, "python")
        try:
            client.start()
            first_pid = client._proc.pid
            assert client._proc.poll() is None

            # 模拟语言服务器崩溃（OOM / 配置错误）
            client._proc.kill()
            client._proc.wait(timeout=5)

            client.start()

            assert client._proc is not None
            assert client._proc.poll() is None, (
                "崩溃的服务器没有被替换：客户端会一直复用死进程"
            )
            assert client._proc.pid != first_pid, "没有真正重新拉起进程"
        finally:
            client.shutdown()

    def test_a_live_server_is_reused(self, tmp_path) -> None:
        """自愈不能变成「每次都重启」，否则每次调用都付一次启动成本。"""
        import sys

        from qwenpaw.agents.tools import _lsp_client as L

        class _NoHandshake(L.LspClient):
            def _initialize(self) -> None:
                return None

        argv = [sys.executable, "-c", "import time; time.sleep(600)"]
        client = _NoHandshake(argv, tmp_path, "python")
        try:
            client.start()
            pid = client._proc.pid
            client.start()
            assert client._proc.pid == pid, "存活的服务被无谓重启"
        finally:
            client.shutdown()


# ---------------------------------------------------------------------------
# 6. ACP 会话清理：单个失败不得连带泄漏其余子进程
# ---------------------------------------------------------------------------


class TestAcpTeardownIsIsolatedAndCannotMask:
    """关闭全部会话时，一个失败不能中止整轮清理。

    ``_close_conversation`` 的 ``finally`` 会杀掉进程树并关闭 async exit stack ——
    两者都可能抛。旧实现直接 ``await`` 在循环里，且会话表已被清空，所以一旦
    抛错，**剩余的 CLI 子进程全部泄漏**且再也无法被回收（只有重启进程）。
    """

    def test_one_failing_session_does_not_abort_the_rest(self) -> None:
        from qwenpaw.agents.acp import service as S

        svc = S.ACPService.__new__(S.ACPService)
        svc._lock = asyncio.Lock()
        svc._sessions = {}

        class _Conv:
            def __init__(self, name: str, boom: bool = False) -> None:
                self.name = name
                self.boom = boom

        for name in ("a", "b", "c"):
            svc._sessions[(f"chat-{name}", "agent")] = _Conv(name)
        svc._sessions[("chat-a", "agent")].boom = True

        closed: list[str] = []

        async def _close(conv) -> None:
            if conv.boom:
                raise RuntimeError("teardown 失败")
            closed.append(conv.name)

        svc._close_conversation = _close  # type: ignore[method-assign]

        asyncio.run(svc.close_all_sessions())

        assert sorted(closed) == ["b", "c"], (
            f"一个会话清理失败导致其余会话被跳过（子进程泄漏）: {closed}"
        )

    def test_source_guards_both_teardown_steps(self) -> None:
        """finally 里的两步都必须各自容错，否则会掩盖原始错误。"""
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        src = (root / "src" / "qwenpaw" / "agents" / "acp" / "service.py").read_text(
            encoding="utf-8",
        )
        start = src.find("async def _close_conversation")
        assert start > 0
        body = src[start:start + 2000]

        assert "_kill_process_tree" in body and "exit_stack.aclose()" in body
        # 两步都应被 try/except 包住
        assert body.count("except Exception") >= 3, (
            "teardown 步骤未加保护：抛错会替换真实异常并跳过后续清理"
        )
