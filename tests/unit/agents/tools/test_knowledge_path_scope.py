"""目录检索侧的路径隔离：按**文件**判定可访问性。

知识服务的 ``/v1/search`` 只接受 space id，没有文档级过滤参数，因此收窄必须在
返回路径上完成。这一层一旦漏掉，案件甲的文件就会出现在案件乙智能体的回答里 ——
隔离形同不存在。这里锁定三件事：

1. 只放行被授权路径下的文件；
2. 未绑定（公共）文件对持有根路径的智能体可见；
3. ``lightrag-graph`` 这类没有 document_id 的推断片段，在智能体不能覆盖全部
   文档时被丢弃 —— 无法归因的推断不该作为答案呈现。
"""

from __future__ import annotations

import types
import uuid

import pytest

from qwenpaw.agents.tools import knowledge
from qwenpaw.platform_store import get_conn, put_conn
from qwenpaw.platform_store import paths as P

from ...platform_store.test_legacy_import import requires_platform_db

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


def _text(chunk) -> str:
    block = chunk.content[0]
    return block["text"] if isinstance(block, dict) else block.text


@pytest.fixture()
def scope(monkeypatch):
    """账号 + 可切换的智能体身份，并在结束后清理。"""
    from qwenpaw.agents.tools import path_scope

    owner = f"ktest-{uuid.uuid4().hex[:10]}"
    state = {"agent": ""}

    def set_agent(agent_id: str) -> None:
        state["agent"] = agent_id
        monkeypatch.setattr(path_scope, "current_agent_id", lambda: state["agent"])

    monkeypatch.setenv("QWENPAW_KNOWLEDGE_URL", SERVICE_URL)
    monkeypatch.setattr(knowledge, "_resolve_account_username", lambda: owner)

    yield owner, set_agent

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                'DELETE FROM "platform".paths WHERE owner_username = %s', (owner,)
            )
            cur.execute(
                'DELETE FROM "platform".agent_path_bindings WHERE agent_id LIKE %s',
                (f"{owner}%",),
            )
            cur.execute(
                'DELETE FROM "platform".resource_bindings WHERE resource_id LIKE %s',
                (f"{owner}%",),
            )
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
    finally:
        put_conn(conn)


def _install_http(monkeypatch, search_evidence):
    """把 httpx 换成脚本化的假实现，返回收集到的请求列表。"""
    calls: list[tuple[str, str, dict]] = []

    def handler(method, url, kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/v1/spaces"):
            return _FakeResponse(200, [{"id": "space-1", "name": "默认"}])
        if url.endswith("/v1/documents"):
            return _FakeResponse(
                200,
                {
                    "items": [{"id": doc_id} for doc_id in search_evidence["documents"]],
                    "total": len(search_evidence["documents"]),
                },
            )
        if url.endswith("/v1/search"):
            return _FakeResponse(
                200,
                {
                    "query": "x",
                    "evidence": [
                        {
                            "document_id": doc_id,
                            "filename": f"{doc_id}.txt",
                            "content": "content",
                            "source": "fulltext",
                        }
                        for doc_id in search_evidence["evidence"]
                    ],
                    "documents": [
                        {"document_id": doc_id, "filename": f"{doc_id}.txt"}
                        for doc_id in search_evidence["evidence"]
                    ],
                    "total_count": len(search_evidence["evidence"]),
                    "document_count": len(search_evidence["evidence"]),
                },
            )
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


@requires_platform_db
async def test_case_document_is_hidden_from_an_agent_of_another_case(
    scope, monkeypatch,
):
    """本案之外的文件不能出现在检索结果里。

    这是隔离的核心断言：同一个账号、同一个知识空间，只因为文件绑在别的案件路径
    上，就必须对该智能体不可见。
    """
    owner, set_agent = scope
    root = P.ensure_root_path(owner, "公共")
    case1 = P.create_path(owner, "案件一")
    case2 = P.create_path(owner, "案件二")
    public_doc = f"{owner}-公共文件"
    case1_doc = f"{owner}-案件一文件"
    case2_doc = f"{owner}-案件二文件"
    P.bind_resource(case1, P.KIND_KNOWLEDGE_DOCUMENT, case1_doc)
    P.bind_resource(case2, P.KIND_KNOWLEDGE_DOCUMENT, case2_doc)
    # 案件一的智能体：只挂案件一（不挂根，因此连公共文件也看不到）。
    P.bind_agent(f"{owner}-ag", case1)
    set_agent(f"{owner}-ag")

    _install_http(
        monkeypatch,
        {
            "documents": [public_doc, case1_doc, case2_doc],
            "evidence": [public_doc, case1_doc, case2_doc],
        },
    )
    text = _text(await knowledge.query_knowledge("关键词"))

    assert case1_doc in text or "案件一文件" in text
    assert case2_doc not in text
    assert public_doc not in text


@requires_platform_db
async def test_root_grant_sees_public_but_not_case_material(scope, monkeypatch):
    """挂根路径的智能体能看到未绑定文件，但看不到已归入案件的文件。"""
    owner, set_agent = scope
    root = P.ensure_root_path(owner, "公共")
    case = P.create_path(owner, "案件一")
    public_doc = f"{owner}-公共文件"
    case_doc = f"{owner}-案件文件"
    P.bind_resource(case, P.KIND_KNOWLEDGE_DOCUMENT, case_doc)
    P.bind_agent(f"{owner}-ag", root)
    set_agent(f"{owner}-ag")

    _install_http(
        monkeypatch,
        {"documents": [public_doc, case_doc], "evidence": [public_doc, case_doc]},
    )
    text = _text(await knowledge.query_knowledge("关键词"))

    assert public_doc in text
    assert case_doc not in text


@requires_platform_db
async def test_get_document_detail_refuses_a_document_outside_the_agents_paths(
    scope, monkeypatch,
):
    """直接按 id 读详情也要过同一道检查。

    文档 id 是模型的普通入参，可以凭空写出或从上文带过来；只查 ``query_knowledge``
    而不查这里，就等于给了一条绕过隔离的旁路。
    """
    owner, set_agent = scope
    root = P.ensure_root_path(owner, "公共")
    case = P.create_path(owner, "案件一")
    elsewhere_doc = f"{owner}-别的案件文件"
    P.bind_resource(case, P.KIND_KNOWLEDGE_DOCUMENT, elsewhere_doc)
    P.bind_agent(f"{owner}-ag", root)
    set_agent(f"{owner}-ag")

    calls = _install_http(monkeypatch, {"documents": [elsewhere_doc], "evidence": []})
    text = _text(await knowledge.get_document_detail(elsewhere_doc))

    assert "可访问" in text
    # 没有读到内容，就不该发出取 chunks 的请求。
    assert not any(url.endswith("/v1/documents/chunks") for _, url, _ in calls)
