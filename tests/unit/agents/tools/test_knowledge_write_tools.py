"""Chat-driven knowledge-space creation and document upload.

The knowledge service runs in its own container, so these tools speak HTTP with
the account identity in headers. The tests replace httpx with a recorder to
check the request shape, the readiness/duplicate handling, and that the caller
cannot upload a file from outside their own workspace.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from qwenpaw.agents.tools import knowledge, user_files

SERVICE_URL = "http://127.0.0.1:18100"


class _FakeHTTPError(Exception):
    pass


class _FakeStatusError(_FakeHTTPError):
    def __init__(self, response):
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = {} if payload is None else payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _FakeStatusError(self)


class _FakeClient:
    def __init__(self, handler):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        return self._handler("GET", url, kwargs)

    async def post(self, url, **kwargs):
        return self._handler("POST", url, kwargs)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("CONSOLE_MEDIA_DIR", raising=False)
    monkeypatch.setenv("QWENPAW_KNOWLEDGE_URL", SERVICE_URL)
    monkeypatch.setattr(user_files, "get_current_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(user_files, "resolve_account_username", lambda: "u1")
    media = tmp_path / "media"
    media.mkdir()
    return media


@pytest.fixture
def http(monkeypatch):
    """Install a fake httpx whose responses are scripted per request."""
    def install(routes):
        calls: list[tuple[str, str, dict]] = []

        def handler(method, url, kwargs):
            calls.append((method, url, kwargs))
            for (m, suffix), response in routes.items():
                if m == method and url.endswith(suffix):
                    if isinstance(response, Exception):
                        raise response
                    return response
            raise AssertionError(f"unexpected request: {method} {url}")

        monkeypatch.setattr(
            knowledge,
            "httpx",
            types.SimpleNamespace(
                AsyncClient=lambda **kwargs: _FakeClient(handler),
                HTTPError=_FakeHTTPError,
                HTTPStatusError=_FakeStatusError,
            ),
        )
        return calls

    return install


def _write(path: Path, text: str = "hello") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _text(chunk) -> str:
    block = chunk.content[0]
    return block["text"] if isinstance(block, dict) else block.text


async def test_create_space_reports_the_new_space(workspace, http):
    http(
        {
            ("GET", "/v1/spaces"): _FakeResponse(200, []),
            ("POST", "/v1/spaces"): _FakeResponse(
                200,
                {"id": "s-1", "name": "采购合同"},
            ),
        },
    )

    text = _text(await knowledge.create_knowledge_space("采购合同"))

    assert "已创建知识空间" in text
    assert "s-1" in text


async def test_create_space_reuses_an_existing_one(workspace, http):
    calls = http({("GET", "/v1/spaces"): _FakeResponse(200, [{"id": "s-9", "name": "采购合同"}])})

    text = _text(await knowledge.create_knowledge_space("采购合同"))

    assert "已存在知识空间" in text
    assert not any(method == "POST" for method, _, _ in calls)


async def test_upload_requires_a_configured_service(monkeypatch, tmp_path):
    monkeypatch.delenv("QWENPAW_KNOWLEDGE_URL", raising=False)
    monkeypatch.setattr(user_files, "get_current_workspace_dir", lambda: tmp_path)

    text = _text(await knowledge.upload_knowledge_document("合同.pdf"))

    assert "尚未配置" in text


async def test_upload_rejects_a_file_outside_the_workspace(workspace, http, tmp_path):
    http({})
    outside = _write(tmp_path.parent / "another-account" / "合同.pdf")

    text = _text(await knowledge.upload_knowledge_document(str(outside)))

    assert "不在你的工作区内" in text


async def test_upload_reports_the_readiness_reason(workspace, http):
    http({("POST", "/v1/documents/upload"): _FakeResponse(409, {"detail": "未配置 Embedding 模型"})})
    _write(workspace / f"{'a' * 32}_合同.pdf")

    text = _text(await knowledge.upload_knowledge_document("合同.pdf"))

    assert "未配置 Embedding 模型" in text


async def test_upload_uses_the_cleaned_attachment_name_and_waits(workspace, http):
    calls = http(
        {
            ("POST", "/v1/documents/upload"): _FakeResponse(
                200,
                {"id": "doc-1", "status": "queued", "filename": "合同.pdf"},
            ),
            ("GET", "/v1/documents/doc-1"): _FakeResponse(
                200,
                {"id": "doc-1", "status": "completed", "filename": "合同.pdf"},
            ),
        },
    )
    _write(workspace / f"{'b' * 32}_合同.pdf", "pdf-bytes")

    text = _text(await knowledge.upload_knowledge_document("合同.pdf"))

    upload_call = next(call for call in calls if call[0] == "POST")
    assert upload_call[2]["files"]["file"][0] == "合同.pdf"
    assert "已上传并入库" in text


async def test_upload_reports_a_duplicate_without_reprocessing(workspace, http):
    http(
        {
            ("POST", "/v1/documents/upload"): _FakeResponse(
                200,
                {"id": "doc-2", "status": "completed", "filename": "合同.pdf", "duplicate": True},
            ),
        },
    )
    _write(workspace / f"{'c' * 32}_合同.pdf")

    text = _text(await knowledge.upload_knowledge_document("合同.pdf"))

    assert "未重复上传" in text


async def test_upload_suggests_creating_an_unknown_space(workspace, http):
    http({("GET", "/v1/spaces"): _FakeResponse(200, [{"id": "s-1", "name": "其他"}])})
    _write(workspace / f"{'d' * 32}_合同.pdf")

    text = _text(await knowledge.upload_knowledge_document("合同.pdf", space_name="不存在"))

    assert "没有找到名为" in text
    assert "create_knowledge_space" in text


async def test_upload_reports_a_failed_parse(workspace, http):
    http(
        {
            ("POST", "/v1/documents/upload"): _FakeResponse(
                200,
                {"id": "doc-3", "status": "queued", "filename": "坏文件.pdf"},
            ),
            ("GET", "/v1/documents/doc-3"): _FakeResponse(
                200,
                {"id": "doc-3", "status": "failed", "error": "解析器不可用"},
            ),
        },
    )
    _write(workspace / f"{'e' * 32}_坏文件.pdf")

    text = _text(await knowledge.upload_knowledge_document("坏文件.pdf"))

    assert "处理失败" in text
    assert "解析器不可用" in text
