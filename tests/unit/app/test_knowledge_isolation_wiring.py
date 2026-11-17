# -*- coding: utf-8 -*-
"""Wiring guards for the knowledge isolation fixes.

These tests exercise the *seams* rather than the retrieval quality:

* every knowledge call is signed and carries no client-controlled admin flag;
* the automatic injection uses the agent's allowed document set (the one path
  that used to read every case in the account);
* a duplicate upload is idempotent and never silently moves a document;
* binding a document to a path verifies that the document belongs to the
  calling account.
"""

from __future__ import annotations

import hashlib
import hmac
import pathlib

import pytest
from fastapi import HTTPException

from qwenpaw.app import knowledge_client
from qwenpaw.app.routers import paths as paths_api

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


# --------------------------------------------------------------------------
# Signed identity
# --------------------------------------------------------------------------


def test_service_headers_are_signed(monkeypatch):
    monkeypatch.setenv("QWENPAW_KNOWLEDGE_IDENTITY_SECRET", "unit-secret")
    monkeypatch.setenv("QWENPAW_KNOWLEDGE_API_KEY", "key-123")

    headers = knowledge_client.service_headers("alice")

    assert headers["X-User-Id"] == "alice"
    assert headers["X-Knowledge-Key"] == "key-123"
    assert "X-Is-Admin" not in headers
    timestamp = int(headers["X-User-Ts"])
    expected = hmac.new(
        b"unit-secret",
        f"alice\n{timestamp}".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert headers["X-User-Sig"] == expected


def test_api_key_is_the_fallback_signing_secret(monkeypatch):
    monkeypatch.delenv("QWENPAW_KNOWLEDGE_IDENTITY_SECRET", raising=False)
    monkeypatch.setenv("QWENPAW_KNOWLEDGE_API_KEY", "key-123")

    assert knowledge_client.identity_secret() == "key-123"
    headers = knowledge_client.service_headers("bob")
    assert "X-User-Sig" in headers


def test_no_identity_is_invented_when_nothing_is_configured(monkeypatch):
    monkeypatch.delenv("QWENPAW_KNOWLEDGE_IDENTITY_SECRET", raising=False)
    monkeypatch.delenv("QWENPAW_KNOWLEDGE_API_KEY", raising=False)

    headers = knowledge_client.service_headers("carol")
    assert headers == {"X-User-Id": "carol"}


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeClient:
    def __init__(self, status_code: int) -> None:
        self._status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, *_args, **_kwargs):
        return _FakeResponse(self._status_code)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [(200, True), (404, False), (500, None)],
)
async def test_document_exists_is_tri_state(monkeypatch, status, expected):
    monkeypatch.setenv("QWENPAW_KNOWLEDGE_URL", "http://knowledge.test")
    monkeypatch.setattr(
        knowledge_client.httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeClient(status),
    )

    assert await knowledge_client.document_exists("alice", "doc-1") is expected


# --------------------------------------------------------------------------
# Automatic injection scope
# --------------------------------------------------------------------------


def test_auto_injection_uses_the_agent_allow_list():
    source = (REPO_ROOT / "src/qwenpaw/agents/react_agent.py").read_text(encoding="utf-8")
    start = source.index("async def _inject_auto_knowledge_context")
    end = source.index("async def _reply", start)
    block = source[start:end]

    assert "visible_document_ids" in block, (
        "automatic injection must resolve the agent's granted documents"
    )
    assert "if not allowed_docs:" in block, (
        "an agent with no grants must get no injection (fail closed)"
    )
    assert '"document_ids": sorted(allowed_docs)' in block, (
        "the allow-list must be sent to the knowledge service"
    )
    assert '"workspaces": allowed_workspaces' in block, (
        "graph evidence must be scoped to the same paths"
    )
    assert "knowledge_http.service_headers" in block, (
        "automatic injection must use the signed header helper"
    )


def test_tool_sends_the_allow_list_too():
    source = (REPO_ROOT / "src/qwenpaw/agents/tools/knowledge.py").read_text(
        encoding="utf-8",
    )
    assert '"document_ids": sorted(allowed_docs)' in source


# --------------------------------------------------------------------------
# Duplicate upload is idempotent
# --------------------------------------------------------------------------


def test_service_dedupe_does_not_overwrite_the_workspace():
    source = (REPO_ROOT / "knowledge-service/app/main.py").read_text(encoding="utf-8")
    start = source.index("    if existing:")
    end = source.index("    created = await asyncio.to_thread(", start)
    block = source[start:end]

    assert "duplicate" in block
    assert "set_document_workspace" not in block, (
        "dedup must not move the existing document's graph workspace"
    )
    assert "requested_workspace" in block, (
        "the response should show that the request asked for a different path"
    )


def test_router_does_not_bind_a_duplicate_into_a_second_path():
    source = (REPO_ROOT / "src/qwenpaw/app/routers/knowledge.py").read_text(
        encoding="utf-8",
    )
    start = source.index("    duplicate = bool(")
    end = source.index("    if path_id and document_id:", start)
    block = source[start:end]

    assert "path_of_resource" in block
    assert "moved" in block
    # The duplicate branch returns before the bind call below it; a
    # `bind_resource(` inside the block would mean the document still moves.
    assert "bind_resource(" not in block


# --------------------------------------------------------------------------
# Binding requires ownership
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_rejects_a_foreign_document(monkeypatch):
    async def _not_owned(_username, _document_id):
        return False

    monkeypatch.setenv("QWENPAW_KNOWLEDGE_URL", "http://knowledge.test")
    monkeypatch.setattr(paths_api.knowledge_http, "document_exists", _not_owned)

    with pytest.raises(HTTPException) as exc:
        await paths_api._assert_resource_owned(
            "alice", "knowledge_document", "someone-elses-doc",
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_bind_fails_closed_when_the_service_is_unreachable(monkeypatch):
    async def _unknown(_username, _document_id):
        return None

    monkeypatch.setenv("QWENPAW_KNOWLEDGE_URL", "http://knowledge.test")
    monkeypatch.setattr(paths_api.knowledge_http, "document_exists", _unknown)

    with pytest.raises(HTTPException) as exc:
        await paths_api._assert_resource_owned(
            "alice", "knowledge_document", "doc-1",
        )
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_bind_accepts_an_owned_document(monkeypatch):
    async def _owned(_username, _document_id):
        return True

    monkeypatch.setenv("QWENPAW_KNOWLEDGE_URL", "http://knowledge.test")
    monkeypatch.setattr(paths_api.knowledge_http, "document_exists", _owned)

    await paths_api._assert_resource_owned(
        "alice", "knowledge_document", "doc-1",
    )


@pytest.mark.asyncio
async def test_bind_rejects_an_unknown_resource_kind():
    with pytest.raises(HTTPException) as exc:
        await paths_api._assert_resource_owned("alice", "not-a-kind", "x")
    assert exc.value.status_code == 400


# --------------------------------------------------------------------------
# Automatic injection: behaviour, not just source text
# --------------------------------------------------------------------------


class _FakeHttpResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Serves the three endpoints the auto-injection calls, and records searches."""

    searches: list[dict] = []
    spaces: list[dict] = [{"id": "space-1", "name": "空间一"}]
    documents: list[dict] = [{"id": "doc-a"}, {"id": "doc-b"}]

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url, **_kwargs):
        if url.endswith("/v1/spaces"):
            return _FakeHttpResponse(self.spaces)
        if url.endswith("/v1/documents"):
            return _FakeHttpResponse({"items": self.documents, "total": len(self.documents)})
        return _FakeHttpResponse({})

    async def post(self, url, json=None, **_kwargs):
        self.searches.append(json or {})
        return _FakeHttpResponse(
            {
                "evidence": [
                    {
                        "id": "chunk-1",
                        "document_id": "doc-a",
                        "filename": "案件A报告.txt",
                        "page": 3,
                        "content": "案件A的关键内容",
                        "source": "semantic",
                        "confidence": "high",
                    }
                ]
            }
        )


def _user_message(text: str):
    from agentscope.message import Msg, TextBlock

    return Msg(
        name="user",
        role="user",
        content=[TextBlock(type="text", text=text)],
    )


@pytest.mark.asyncio
async def test_auto_injection_sends_only_granted_documents(monkeypatch):
    import httpx

    from qwenpaw.agents.react_agent import QwenPawAgent
    from qwenpaw.agents.tools import knowledge as tool_module

    monkeypatch.setenv("QWENPAW_KNOWLEDGE_URL", "http://knowledge.test")
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(tool_module, "_resolve_account_username", lambda: "alice")
    monkeypatch.setattr(tool_module, "_workspaces_for_agent", lambda _username: ["ws_case_a"])
    monkeypatch.setattr(
        "qwenpaw.agents.tools.path_scope.visible_document_ids",
        lambda _username, _all_docs: ["doc-a"],
    )
    _FakeAsyncClient.searches = []

    class _State:
        context = [_user_message("案件A的材料讲了什么？")]

    from types import SimpleNamespace

    agent = SimpleNamespace(
        state=_State(),
        _knowledge_auto_context_key=None,
    )

    await QwenPawAgent._inject_auto_knowledge_context(agent)

    assert _FakeAsyncClient.searches, "the injection must query the service"
    sent = _FakeAsyncClient.searches[0]
    assert sent["document_ids"] == ["doc-a"]
    assert sent["workspaces"] == ["ws_case_a"]
    # The rendered snippet was appended as a tagged, hidden user message.
    assert len(agent.state.context) == 2
    appended = agent.state.context[1]
    assert appended.name == "knowledge_auto_context"
    assert "案件A报告.txt" in appended.content[0].text


@pytest.mark.asyncio
async def test_auto_injection_is_fail_closed_without_grants(monkeypatch):
    import httpx

    from qwenpaw.agents.react_agent import QwenPawAgent
    from qwenpaw.agents.tools import knowledge as tool_module

    monkeypatch.setenv("QWENPAW_KNOWLEDGE_URL", "http://knowledge.test")
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(tool_module, "_resolve_account_username", lambda: "alice")
    monkeypatch.setattr(tool_module, "_workspaces_for_agent", lambda _username: [])
    monkeypatch.setattr(
        "qwenpaw.agents.tools.path_scope.visible_document_ids",
        lambda _username, _all_docs: [],
    )
    _FakeAsyncClient.searches = []

    class _State:
        context = [_user_message("案件B的材料讲了什么？")]

    from types import SimpleNamespace

    agent = SimpleNamespace(
        state=_State(),
        _knowledge_auto_context_key=None,
    )

    await QwenPawAgent._inject_auto_knowledge_context(agent)

    assert _FakeAsyncClient.searches == [], "no grants must mean no retrieval"
    assert len(agent.state.context) == 1, "nothing may be injected"


# --------------------------------------------------------------------------
# Explicit copy is the supported way to share material across cases
# --------------------------------------------------------------------------


def test_upload_copy_skips_content_dedup():
    source = (REPO_ROOT / "knowledge-service/app/main.py").read_text(encoding="utf-8")
    start = source.index("    content_hash = content_digest.hexdigest()")
    end = source.index("    if existing:", start)
    block = source[start:end]

    assert "if not copy:" in block
    assert "find_document_by_hash" in block
    assert "copy: bool = Form(default=False)" in source


def test_router_forwards_the_copy_flag():
    source = (REPO_ROOT / "src/qwenpaw/app/routers/knowledge.py").read_text(
        encoding="utf-8",
    )
    assert 'copy: bool = Form(default=False)' in source
    assert '"copy": "true" if copy else "false"' in source


def test_frontend_exposes_an_explicit_copy_choice():
    source = (REPO_ROOT / "console/src/pages/Knowledge/index.tsx").read_text(
        encoding="utf-8",
    )
    assert "copyDuplicates" in source
    assert "重复文件复制到当前路径" in source
    api = (REPO_ROOT / "console/src/api/modules/knowledge.ts").read_text(
        encoding="utf-8",
    )
    assert 'form.append("copy"' in api
