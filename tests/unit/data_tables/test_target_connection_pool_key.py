# -*- coding: utf-8 -*-
"""返回连接必须回到它被借出的那个池。

池按 ``(db_name, schema)`` 分键。早期实现用「调用方声明的 schema」去算键，只要
调用方借出时带了 schema、归还时忘了带，就会：

1. 归还落到未限域池（或直接 close），限域池仍把这条连接记为已借出 —— 借满 2 条后
   该 schema 上的查询开始报「连接池已满」，表现为「工具时好时坏」；
2. 更糟的情况下连接被塞进另一个池，把 ``search_path`` 一起带过去 —— 这是隔离上的
   隐患，而不只是可用性问题。

修复办法是不再相信参数：池建库时就把 schema 写进了连接的启动选项
（``options="-c search_path=..."``），归还时从连接自身读回来。

这里锁定两件事：键能从连接恢复；参数与连接不一致时以连接为准（并且不会被塞进
错误的池）。
"""
from __future__ import annotations

import pytest

from qwenpaw.data_tables import connection as conn_mod


class _FakeConn:
    """Minimal stand-in exposing get_dsn_parameters like psycopg2 does."""

    def __init__(self, options: str | None = "-c search_path=public", **kw):
        self._options = options
        self._closed = False
        self._params = kw

    def get_dsn_parameters(self):
        if self._closed:
            raise Exception("connection already closed")
        return {**self._params, "options": self._options or ""}

    def close(self):
        self._closed = True

    @property
    def closed(self):
        return self._closed


@pytest.fixture(autouse=True)
def _clean_pools():
    """Isolate the module-level pool registry between tests."""
    conn_mod._target_pool.clear()
    conn_mod._target_pool_order.clear()
    yield
    conn_mod._target_pool.clear()
    conn_mod._target_pool_order.clear()


class _RecordingPool:
    def __init__(self):
        self.returned: list[object] = []

    def putconn(self, conn):
        self.returned.append(conn)


class TestSchemaKeyFromConnection:
    def test_scoped_connection_reports_its_schema(self) -> None:
        conn = _FakeConn("-c search_path=p_abc123")
        assert conn_mod._schema_key_from_connection(conn) == "p_abc123"

    def test_unscoped_connection_reports_empty(self) -> None:
        assert conn_mod._schema_key_from_connection(_FakeConn("")) == ""
        assert conn_mod._schema_key_from_connection(_FakeConn(None)) == ""

    def test_other_startup_options_do_not_confuse_the_parse(self) -> None:
        conn = _FakeConn("-c statement_timeout=5000 -c search_path=p_x")
        assert conn_mod._schema_key_from_connection(conn) == "p_x"

    def test_comma_joined_search_path_is_recovered_whole(self) -> None:
        """A list form (case schema + public) must round-trip identically.

        The pool key uses the same joined string, so any truncation here would
        split one pool into two and leak the other's connections.
        """
        joined = "p_abc,public"
        conn = _FakeConn(f"-c search_path={joined}")
        assert conn_mod._schema_key_from_connection(conn) == joined

    def test_closed_connection_reports_empty_instead_of_raising(self) -> None:
        conn = _FakeConn("-c search_path=p_x")
        conn.close()
        assert conn_mod._schema_key_from_connection(conn) == ""


class TestReturnGoesToTheRightPool:
    def test_forgotten_schema_argument_still_returns_to_the_scoped_pool(self) -> None:
        """The exact defect: checkout with schema, return without it.

        The connection must land in the scoped pool, not the unscoped one.
        """
        scoped = _RecordingPool()
        unscoped = _RecordingPool()
        conn_mod._target_pool[("db", "public")] = scoped
        conn_mod._target_pool[("db", "")] = unscoped

        conn = _FakeConn("-c search_path=public")
        conn_mod.put_target_connection(conn, "db")  # schema omitted

        assert scoped.returned == [conn], "went to the wrong pool"
        assert unscoped.returned == [], "connection leaked into the unscoped pool"

    def test_mismatched_argument_trusts_the_connection(self) -> None:
        """A wrong declared schema must not misroute the connection."""
        scoped = _RecordingPool()
        other = _RecordingPool()
        conn_mod._target_pool[("db", "p_real")] = scoped
        conn_mod._target_pool[("db", "p_other")] = other

        conn = _FakeConn("-c search_path=p_real")
        conn_mod.put_target_connection(conn, "db", "p_other")

        assert scoped.returned == [conn]
        assert other.returned == []

    def test_no_matching_pool_closes_instead_of_leaking(self) -> None:
        conn = _FakeConn("-c search_path=p_gone")
        conn_mod.put_target_connection(conn, "db", "p_gone")
        assert conn.closed, "a reclaimed pool must not leak its connection"


class TestCallSitesPassTheSchemaBothWays:
    """Static check: every schema-scoped checkout repeats it on return.

    The pool-level fix above makes a forgotten argument harmless for the *pool*,
    but the argument is still the documented contract and the cross-check logs an
    error for it. Keep the call sites honest so the log stays quiet in production.
    """

    def test_source_pairs_are_consistent(self) -> None:
        import pathlib
        import re

        src = pathlib.Path(conn_mod.__file__).resolve().parents[1]  # src/qwenpaw
        files = [
            src / "agents" / "tools" / "data_tables.py",
            src / "agents" / "tools" / "graph_analysis.py",
            src / "data_tables" / "preview_api.py",
            src / "data_tables" / "importer.py",
            src / "data_tables" / "connection.py",
        ]
        # (function body text) -> the two calls it makes must agree on argc.
        # ``put_target_connection`` takes the connection as its first argument,
        # so its argument count is exactly one more than the matching checkout.
        get_re = re.compile(r"get_target_connection\(\s*([^)]*)\)")
        put_re = re.compile(r"put_target_connection\(\s*([^)]*)\)")
        problems: list[str] = []
        for path in files:
            text = path.read_text(encoding="utf-8")
            gets = [m.group(1) for m in get_re.finditer(text)]
            puts = [m.group(1) for m in put_re.finditer(text)]
            get_argc = sorted(len(g.split(",")) for g in gets)
            put_argc = sorted(len(p.split(",")) - 1 for p in puts)
            if get_argc != put_argc:
                problems.append(f"{path.name}: checkout arg counts {get_argc} != return {put_argc}")
        assert not problems, "; ".join(problems)


class _SlowPool:
    """Pool that only frees its single connection after a short delay."""

    def __init__(self, delay: float = 0.15):
        self.delay = delay
        self.maxconn = 1
        self._used: dict = {}
        self._pool = [object()]  # seed the single connection
        self._lock = __import__("threading").Lock()

    def getconn(self):
        with self._lock:
            if self._pool:
                conn = self._pool.pop()
                self._used[id(conn)] = conn
                return conn
            raise conn_mod.PoolError("connection pool exhausted")

    def putconn(self, conn):
        import time as _t
        _t.sleep(self.delay)
        with self._lock:
            self._used.pop(id(conn), None)
            self._pool.append(conn)


class TestCheckoutWaitsForBusyPool:
    """并发读同一张表时，池满应该排队等待而不是直接 500。

    池上限是 2（为了让所有账号 × 路径的连接总数低于服务端上限），所以「三个人同时
    打开同一个案件的表」这类正常并发会撞到上限。旧行为是 psycopg2 立刻抛
    ``PoolError: connection pool exhausted``，表现为 HTTP 500。

    测试走 ``get_target_connection`` 这个真实入口（而不是直接调
    ``_checkout_with_wait``），这样「入口忘了接上等待逻辑」这种回退也会被抓住。
    """

    def test_second_checkout_waits_instead_of_failing(self, monkeypatch) -> None:
        pool = _SlowPool(delay=0.2)
        first = pool.getconn()

        monkeypatch.setattr(conn_mod, "_target_pool", {("db", "public"): pool})
        monkeypatch.setattr(conn_mod, "_target_pool_order", [("db", "public")])
        monkeypatch.setattr(
            conn_mod, "_pin_search_path", lambda _c, _s=None: None,
        )

        # 第二个借出必须等到归还，而不是立刻报错。
        def _release_later():
            import time as _t
            _t.sleep(0.05)
            pool.putconn(first)

        import threading
        threading.Thread(target=_release_later, daemon=True).start()

        second = conn_mod.get_target_connection("db", "public")
        assert second is not None

    def test_timeout_still_raises_with_actionable_message(self, monkeypatch) -> None:
        """等待有上界；超时后的报错要能让人看懂怎么办。"""
        monkeypatch.setattr(conn_mod, "_POOL_WAIT_SECONDS", 0.2)
        pool = _SlowPool(delay=5.0)
        pool.getconn()  # 占住唯一一条

        monkeypatch.setattr(conn_mod, "_target_pool", {("db", "p_x"): pool})
        monkeypatch.setattr(conn_mod, "_target_pool_order", [("db", "p_x")])
        monkeypatch.setattr(
            conn_mod, "_pin_search_path", lambda _c, _s=None: None,
        )

        with pytest.raises(conn_mod.PoolError) as exc:
            conn_mod.get_target_connection("db", "p_x")

        message = str(exc.value)
        assert "连接池已满" in message
        assert "db" in message
