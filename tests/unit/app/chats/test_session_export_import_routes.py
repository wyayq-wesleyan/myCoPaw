# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,protected-access,unused-argument
"""``GET /chats/{id}/export`` 与 ``POST /chats/import`` 的路由层契约。

这一层不碰数据库（HistoryStore 的往返契约在
``tests/unit/agents/context/test_history_session_export_import.py`` 里用真库验证），
只盯住路由行为本身：鉴权、文件校验、以及**导入生成的身份必须是全新的**。

身份这条是重点。导出文件里刻意不写 ``id`` / ``session_id`` / ``user_id`` / owner，
导入时全部重新生成——否则一个文件就能用来冒充或覆盖已有会话。测试里专门对
payload 做「不含这些键」的断言，并断言导入后这些值都不是文件里的值。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from qwenpaw.app.chats import api as api_module
from qwenpaw.app.chats.api import router as chats_router
from qwenpaw.app.chats.models import ChatSpec

AGENT_ID = "agent-a"
OWNER = "alice"


def _profile(owner: str = OWNER, strategy: str = "scroll") -> MagicMock:
    """A fake agent profile that satisfies both ownership and strategy lookups."""
    profile = MagicMock(name="Profile")
    profile.owner_username = owner
    profile.running.light_context_config.strategy = strategy
    profile.running.light_context_config.scroll_config.db_filename = "history.db"
    return profile


@pytest.fixture
def workspace(tmp_path: Path) -> MagicMock:
    ws = MagicMock(name="Workspace")
    ws.agent_id = AGENT_ID
    ws.workspace_dir = str(tmp_path)
    return ws


@pytest.fixture
def chat_spec() -> ChatSpec:
    return ChatSpec(
        id="chat-uuid-1",
        name="季度复盘",
        session_id="console:alice:1",
        user_id="alice",
        channel="console",
    )


@pytest.fixture
def manager(chat_spec: ChatSpec) -> MagicMock:
    mgr = MagicMock(name="ChatManager")
    mgr.get_chat = AsyncMock(return_value=chat_spec)
    mgr.create_chat = AsyncMock(side_effect=lambda spec: spec)
    return mgr


@pytest.fixture
def session(tmp_path: Path) -> MagicMock:
    """A fake session whose save path is a real file under tmp_path."""
    fake = MagicMock(name="SafeJSONSession")
    fake.get_session_state_dict = AsyncMock(
        return_value={"agent": {"state": {"context": [{"role": "user"}]}}},
    )
    fake._get_save_path = lambda session_id, user_id, channel="": str(
        tmp_path / f"{user_id}_{session_id.replace(':', '-')}.json"
    )
    return fake


@pytest.fixture
def client(workspace, manager, session) -> TestClient:
    app = FastAPI()
    app.include_router(chats_router, prefix="/api")
    app.dependency_overrides[api_module.get_workspace] = lambda: workspace
    app.dependency_overrides[api_module.get_chat_manager] = lambda: manager
    app.dependency_overrides[api_module.get_session] = lambda: session
    return TestClient(app)


@pytest.fixture
def as_alice():
    """Authenticate as the agent's owner and hand back the fake config."""
    config = MagicMock(name="Config")
    config.agents.profiles = {AGENT_ID: _profile()}
    with patch.object(api_module, "get_request_username", return_value=OWNER), \
         patch.object(api_module, "load_config", return_value=config):
        yield config


def _transcript_rows() -> list[dict]:
    """Rows shaped like ``export_session_rows`` really returns them.

    Including ``session_id`` and ``agent_id`` is the point: a first version of
    these tests used a hand-written row without them, so the assertion that the
    file carries no identity passed while every real export shipped the
    exporter's session and agent ids. Test data has to match the producer's
    output, not a convenient subset of it.
    """
    return [
        {
            "seq": 7,
            "session_id": "console:alice:1",
            "agent_id": AGENT_ID,
            "kind": "model_turn",
            "role": "user",
            "content": "之前的对话",
            "created_at": "2026-10-01T00:00:00+00:00",
            "dedup_key": "msg-1",
        },
    ]


def _export_payload(name: str = "季度复盘") -> dict:
    """A file body, deliberately carrying identity fields it must not own.

    Real exports omit them (see ``test_export_carries_no_identity``), but a file
    is user-supplied input: it may be hand-edited, or produced by an older or a
    hostile build. Stamping them in here is what makes "the importer ignores the
    file's identity" testable at all — without them the mutation that *reads*
    them still passes, because there is nothing to read.
    """
    return {
        "format": api_module._SESSION_FILE_FORMAT,
        "version": api_module._SESSION_FILE_VERSION,
        "exported_at": "2026-10-14T00:00:00+00:00",
        "chat": {
            "name": name,
            "channel": "console",
            "id": "attacker-chosen-id",
            "session_id": "console:bob:9",
            "user_id": "bob",
            "owner_username": "bob",
        },
        "agent": {"agent_id": AGENT_ID},
        "session": {
            "strategy": "scroll",
            "state": {"agent": {"state": {"context": [{"role": "user"}]}}},
            "transcript": _transcript_rows(),
        },
    }


def _upload(client: TestClient, payload, name: str = "chat.session.json"):
    body = (
        payload
        if isinstance(payload, bytes)
        else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    )
    return client.post(
        "/api/chats/import",
        files={"file": (name, body, "application/json")},
    )


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


class TestExport:
    def test_export_carries_no_identity(self, client, as_alice) -> None:
        """文件里不能出现能在别人账号里落地的身份字段。

        用**真实形状的行**（带 ``session_id`` / ``agent_id``）来导出，否则这条断言
        测不到真正的泄漏面。
        """
        fake_store = MagicMock(name="HistoryStore")
        fake_store.export_session_rows.return_value = _transcript_rows()
        with patch(
            "qwenpaw.config.config.load_agent_config",
            return_value=TestStrategyComesFromTheAgentConfig
            ._agent_light_context_config("scroll"),
        ), patch(
            "qwenpaw.agents.context.scroll.history.HistoryStore",
            return_value=fake_store,
        ):
            response = client.get("/api/chats/chat-uuid-1/export")

        assert response.status_code == 200
        payload = response.json()
        dumped = json.dumps(payload, ensure_ascii=False)

        assert payload["format"] == api_module._SESSION_FILE_FORMAT
        assert payload["chat"]["name"] == "季度复盘"
        assert "chat-uuid-1" not in dumped, "导出的文件里带上了 chat id"
        assert "console:alice:1" not in dumped, "导出的文件里带上了源 session_id"
        assert "alice" not in dumped, "导出的文件里带上了用户标识"
        assert f'"agent_id": "{AGENT_ID}"' not in dumped, (
            "导出的文件里带上了源 agent_id（行里带的，导入时会被覆盖，"
            "但没必要泄露出去）"
        )
        # 会话内容本身当然要留着，否则这条断言会以「什么都没导出」的方式通过。
        assert "之前的对话" in dumped

    def test_export_sets_a_downloadable_filename(self, client, as_alice) -> None:
        response = client.get("/api/chats/chat-uuid-1/export")

        disposition = response.headers["content-disposition"]
        assert disposition.startswith("attachment;")
        # 中文名必须走 RFC 5987 的 filename*，否则浏览器拿到乱码。
        assert "filename*=UTF-8''" in disposition
        assert ".session.json" in disposition

    def test_export_includes_the_durable_transcript(self, client, as_alice) -> None:
        """默认是 scroll 策略：历史在行里，不在 state 里。"""
        rows = [{"seq": 1, "kind": "model_turn", "content": "行里的历史"}]
        fake_store = MagicMock(name="HistoryStore")
        fake_store.export_session_rows.return_value = rows
        with patch(
            "qwenpaw.config.config.load_agent_config",
            return_value=TestStrategyComesFromTheAgentConfig
            ._agent_light_context_config("scroll"),
        ), patch(
            "qwenpaw.agents.context.scroll.history.HistoryStore",
            return_value=fake_store,
        ):
            response = client.get("/api/chats/chat-uuid-1/export")

        payload = response.json()
        assert payload["session"]["transcript"] == rows
        assert payload["session"]["strategy"] == "scroll"
        assert fake_store.export_session_rows.call_args[0][0] == "console:alice:1"

    def test_export_works_without_a_user_when_auth_is_disabled(
        self, client, as_alice,
    ) -> None:
        """无鉴权部署（离线单用户）必须能用。

        认证开启时中间件已经在前一步把未登录请求挡掉了，所以走到处理器时没有用户名
        只意味着「这个安装压根没有登录页」。这里要求 401 的话，恰好会让唯一没有登录
        界面的那套部署用不了导出——而它正是要导出的那套。
        """
        with patch.object(api_module, "get_request_username", return_value=None):
            response = client.get("/api/chats/chat-uuid-1/export")

        assert response.status_code == 200

    def test_rejects_a_chat_owned_by_someone_else(self, client, as_alice) -> None:
        as_alice.agents.profiles[AGENT_ID] = _profile(owner="bob")

        response = client.get("/api/chats/chat-uuid-1/export")

        assert response.status_code == 403

    def test_unknown_chat_is_404(self, client, as_alice, manager) -> None:
        manager.get_chat = AsyncMock(return_value=None)

        response = client.get("/api/chats/nope/export")

        assert response.status_code == 404


class TestStrategyComesFromTheAgentConfig:
    """策略必须取自 ``load_agent_config``，而不是全局 profile。

    全局 ``load_config().agents.profiles[...]`` 上 ``light_context_config`` 是未设置
    的（``strategy`` 为 ``None``）；真正生效的值在每 agent 的配置文件里，Scroll 管理
    器读的也是那一份。取错来源会把每个 agent 都判成「非 scroll」，于是**每一次导出
    都静默丢掉整段历史**——文件看着正常，内容却只剩最后一个上下文窗口。
    """

    @staticmethod
    def _agent_light_context_config(strategy: str):
        config = MagicMock(name="AgentConfig")
        config.running.light_context_config.strategy = strategy
        config.running.light_context_config.scroll_config.db_filename = "history.db"
        return config

    def test_transcript_is_exported_when_the_agent_says_scroll(
        self, client, as_alice,
    ) -> None:
        rows = [{"seq": 1, "kind": "model_turn", "content": "行里的历史"}]
        fake_store = MagicMock(name="HistoryStore")
        fake_store.export_session_rows.return_value = rows
        # 全局 profile 照实际那样「没有」这个配置值。
        as_alice.agents.profiles[AGENT_ID] = MagicMock(
            name="GlobalProfile", owner_username=OWNER,
        )
        with patch(
            "qwenpaw.config.config.load_agent_config",
            return_value=self._agent_light_context_config("scroll"),
        ), patch(
            "qwenpaw.agents.context.scroll.history.HistoryStore",
            return_value=fake_store,
        ):
            response = client.get("/api/chats/chat-uuid-1/export")

        payload = response.json()
        assert payload["session"]["strategy"] == "scroll"
        assert payload["session"]["transcript"] == rows, (
            "策略取错了来源：transcript 被静默丢弃"
        )

    def test_non_scroll_agent_still_exports_state_only(
        self, client, as_alice,
    ) -> None:
        as_alice.agents.profiles[AGENT_ID] = MagicMock(
            name="GlobalProfile", owner_username=OWNER,
        )
        with patch(
            "qwenpaw.config.config.load_agent_config",
            return_value=self._agent_light_context_config("native"),
        ):
            response = client.get("/api/chats/chat-uuid-1/export")

        payload = response.json()
        assert payload["session"]["strategy"] == "native"
        assert payload["session"]["transcript"] == []


class TestDurableTranscriptNeedsNoFile:
    """历史早就迁到 PostgreSQL，``history.db`` 只是个地址。

    读取侧原先用 ``db_path.is_file()`` 做守卫。迁移之后那个文件只在「老到迁过库」的
    机器上才存在，于是**每一台全新安装都静默退回上下文窗口**——被淘汰的轮次又从控制台
    消失，正是这个函数当初要修的那个症状。
    """

    def test_transcript_is_read_even_though_no_file_exists(
        self, tmp_path: Path,
    ) -> None:
        workspace = MagicMock(name="Workspace")
        workspace.agent_id = AGENT_ID
        workspace.workspace_dir = str(tmp_path)
        spec = MagicMock(name="ChatSpec")
        spec.id = "chat-uuid-1"
        spec.session_id = "console:alice:1"

        fake_store = MagicMock(name="HistoryStore")
        fake_store.load_session_messages.return_value = ["msg"]
        assert not (tmp_path / "history.db").exists(), "前提：文件不存在"

        with patch(
            "qwenpaw.config.config.load_agent_config",
            return_value=TestStrategyComesFromTheAgentConfig
            ._agent_light_context_config("scroll"),
        ), patch(
            "qwenpaw.agents.context.scroll.history.HistoryStore",
            return_value=fake_store,
        ):
            result = api_module._load_durable_transcript(workspace, spec)

        assert result == ["msg"], (
            "历史没有被读取：存在性守卫把全新安装挡回了上下文窗口"
        )
        fake_store.load_session_messages.assert_called_once_with(
            "console:alice:1", AGENT_ID,
        )


class TestShareKeepsItsOwnRequirement:
    """分享与导出/导入共用属主判据，但只有分享必须要求具名发起者。"""

    def test_share_still_needs_a_named_sender(self, client, as_alice) -> None:
        with patch.object(api_module, "get_request_username", return_value=""):
            response = client.post(
                "/api/chats/chat-uuid-1/share",
                json={"target_usernames": ["bob"]},
            )

        assert response.status_code == 401, (
            "分享需要把快照归属到发起者、并把发起者从收件人里排除；"
            "没有具名调用者时不能继续"
        )


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


class TestImport:
    def test_import_mints_a_brand_new_identity(
        self, client, as_alice, tmp_path: Path,
    ) -> None:
        """导入后 id / session_id / user_id 全都要是新的，**不能沿用文件里的值**。

        文件里的这几个字段是攻击面（``_export_payload`` 故意带上了）：沿用它们
        就能覆盖或冒充已有会话。
        """
        response = _upload(client, _export_payload())

        assert response.status_code == 200
        spec = response.json()
        assert spec["id"] != "chat-uuid-1"
        assert spec["id"] != "attacker-chosen-id", "沿用了文件里的 chat id"
        assert spec["session_id"].startswith("console:imported:")
        assert spec["session_id"] != "console:alice:1"
        assert spec["session_id"] != "console:bob:9", "沿用了文件里的 session_id"
        assert spec["user_id"] == "default", (
            f"沿用了文件里的用户标识：{spec['user_id']!r}"
        )
        assert spec["name"] == "季度复盘"
        assert spec["meta"]["imported_from_file"] is True

    def test_import_writes_the_state_file(self, client, as_alice, session) -> None:
        response = _upload(client, _export_payload())

        spec = response.json()
        path = Path(session._get_save_path(spec["session_id"], "default", spec["channel"]))
        assert path.is_file(), "导入没有写出会话状态文件"
        assert json.loads(path.read_text("utf-8"))["agent"]["state"]["context"]

    def test_import_forces_the_console_channel(
        self, client, as_alice, session,
    ) -> None:
        """文件里的 channel 不参与决定落盘位置，也不该让会话看起来来自别的渠道。"""
        payload = _export_payload()
        payload["chat"]["channel"] = "../../etc"

        response = _upload(client, payload)

        spec = response.json()
        assert spec["channel"] == "console"
        # 落盘路径必须仍在 save_dir 之内。
        path = Path(session._get_save_path(spec["session_id"], "default", spec["channel"]))
        assert str(path).startswith(str(Path(session._get_save_path(
            spec["session_id"], "default", "console",
        )).parent))

    def test_import_rejects_a_non_json_upload(self, client, as_alice) -> None:
        response = _upload(client, b"\x00\x01 not json")
        assert response.status_code == 400

    def test_import_rejects_a_foreign_json_file(self, client, as_alice) -> None:
        response = _upload(client, {"hello": "world"})
        assert response.status_code == 400
        assert "format marker" in response.json()["detail"]

    def test_import_rejects_an_unsupported_version(self, client, as_alice) -> None:
        payload = _export_payload()
        payload["version"] = 99

        response = _upload(client, payload)

        assert response.status_code == 400
        assert "version" in response.json()["detail"]

    def test_import_rejects_a_payload_without_state(self, client, as_alice) -> None:
        payload = _export_payload()
        payload["session"]["state"] = "not a dict"

        response = _upload(client, payload)

        assert response.status_code == 400

    def test_import_rejects_an_oversized_file(
        self, client, as_alice, monkeypatch,
    ) -> None:
        # 真造一个 64MB 文件没意义，把上限调小来打这个分支。
        monkeypatch.setattr(api_module, "_SESSION_IMPORT_MAX_BYTES", 64)

        response = _upload(client, _export_payload())

        assert response.status_code == 413

    def test_import_works_without_a_user_when_auth_is_disabled(
        self, client, as_alice,
    ) -> None:
        """同导出：没有登录页的部署也得能导入。"""
        with patch.object(api_module, "get_request_username", return_value=None):
            response = _upload(client, _export_payload())

        assert response.status_code == 200

    def test_import_rejects_an_agent_owned_by_someone_else(
        self, client, as_alice,
    ) -> None:
        as_alice.agents.profiles[AGENT_ID] = _profile(owner="bob")

        response = _upload(client, _export_payload())

        assert response.status_code == 403

    def test_import_without_a_chat_name_still_succeeds(
        self, client, as_alice,
    ) -> None:
        payload = _export_payload()
        payload["chat"] = {}

        response = _upload(client, payload)

        assert response.status_code == 200
        assert response.json()["name"] == "Imported chat"

    def test_a_transcript_write_failure_names_its_cause(
        self, client, as_alice,
    ) -> None:
        """写历史失败时，报错必须说出底层原因。

        线上就是被这一句笼统的话挡住过：``Could not write the imported
        conversation history`` 没说是为什么，只能去服务器日志里翻。同一条消息
        后面接上 ``relation "platform.history_seq" does not exist`` 才是可执行的。
        这是调用者对自己文件的操作，原因基本都出在部署上，不是文件。
        """
        as_alice.agents.profiles[AGENT_ID] = MagicMock(
            name="GlobalProfile", owner_username=OWNER,
        )
        fake_store = MagicMock(name="HistoryStore")
        fake_store.import_session_rows.side_effect = RuntimeError(
            'relation "platform.history_seq" does not exist',
        )
        with patch(
            "qwenpaw.config.config.load_agent_config",
            return_value=TestStrategyComesFromTheAgentConfig
            ._agent_light_context_config("scroll"),
        ), patch(
            "qwenpaw.agents.context.scroll.history.HistoryStore",
            return_value=fake_store,
        ):
            response = _upload(client, _export_payload())

        assert response.status_code == 500
        detail = response.json()["detail"]
        assert "history_seq" in detail, f"报错没带上真正的原因：{detail!r}"

    def test_a_failed_registration_leaves_no_orphan_file(
        self, client, as_alice, manager, session,
    ) -> None:
        """注册失败时必须清掉已写的状态文件，不留一个用户看不见的孤儿。"""
        manager.create_chat = AsyncMock(side_effect=RuntimeError("boom"))

        response = _upload(client, _export_payload())

        assert response.status_code == 500
        sessions_dir = Path(session._get_save_path("x", "default")).parent
        leftovers = [p for p in sessions_dir.glob("*") if p.is_file()]
        assert leftovers == [], f"留下了孤儿文件：{leftovers}"
