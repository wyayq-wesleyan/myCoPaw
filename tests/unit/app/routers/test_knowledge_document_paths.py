# -*- coding: utf-8 -*-
"""知识文档列表的路径过滤。

隔离在列表侧的落点很窄但很关键：``path_id`` 一旦被漏掉或被别的过滤条件覆盖，
案件外的文件就会出现在列表里。这里锁定两个真实发生过的缺陷：

1. 筛选条件（关键词检索得到的 ``doc_ids``）与 ``path_id`` 同时到达时，后者直接
   覆盖了前者 —— 结果是「在案件里搜索关键词」返回该案件的全部文件，看起来像是
   搜到了，其实关键词完全没生效。
2. 归属校验：外部路径必须返回空页，而不是退化成「不过滤」。
"""

from __future__ import annotations

import types
import uuid

import pytest

from qwenpaw.app.routers import knowledge as api
from qwenpaw.platform_store import get_conn, put_conn
from qwenpaw.platform_store import paths as P

from tests.unit.platform_store.test_legacy_import import (  # noqa: E402
    requires_platform_db,
)


class _Req:
    def __init__(self, user: str | None) -> None:
        self.state = type("S", (), {"user": user})()
        self.headers: dict[str, str] = {}


class _FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


@pytest.fixture()
def account(monkeypatch):
    """一个全新的账号 + 结束后清理路径与绑定。

    ``_request_username`` 走 Bearer token（未启用鉴权时固定返回 ``admin``），不接受
    测试用的 Request 替身，所以这里直接替换解析函数 —— 否则路径归属校验会把测试
    账号的路径当成别人的，全部返回空页。
    """
    owner = f"kdoc-{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(api, "_request_username", lambda request: owner)
    yield owner
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # The foreign-path test creates a second owner derived from this name
            # (``f"{owner}-other"``), so match by prefix rather than by equality —
            # otherwise a stray root path survives every run and accumulates.
            cur.execute(
                'DELETE FROM "platform".paths WHERE owner_username LIKE %s',
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


def _install_http(monkeypatch, documents, pending=0):
    """Record the requests the router makes, and answer with *documents*.

    ``pending`` is what the account-wide pending-count endpoint reports; it is a
    separate call from the listing precisely so it can be non-zero even when the
    listing itself is short-circuited.
    """
    sent: list[dict] = []
    pending_calls: list[str] = []

    def handler(method, url, kwargs):
        if url.endswith("/v1/jobs/pending-count"):
            pending_calls.append(url)
            return _FakeResponse({"pending": pending})
        if url.endswith("/v1/documents"):
            sent.append(kwargs.get("params") or {})
            return _FakeResponse(
                {"items": documents, "total": len(documents), "pending_count": pending}
            )
        raise AssertionError(f"unexpected request: {method} {url}")

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            return handler("GET", url, kwargs)

    monkeypatch.setattr(
        api, "httpx", types.SimpleNamespace(AsyncClient=lambda **kw: _Client(**kw))
    )
    monkeypatch.setenv("QWENPAW_KNOWLEDGE_URL", "http://127.0.0.1:18100")
    # A dict, not a bare list: callers also need to assert on the pending-count
    # calls, and a list cannot carry that extra attribute.
    return {"listing": sent, "pending_calls": pending_calls}


@requires_platform_db
async def test_keyword_filter_and_path_filter_are_intersected(
    account, monkeypatch,
):
    """关键词结果与路径文件取交集，而不是互相覆盖。

    ``/v1/documents`` 只接受一个 ``doc_ids`` 列表，两处收窄必须在这里合并。
    案情刻意设成「路径里的文件多于关键词命中」，否则两者恰好一致，覆盖与取交集
    结果相同，测试就失去分辨力：让 path 覆盖 keyword 会把「搜索」变成「列出该
    路径全部文件」。
    """
    owner = account
    case = P.create_path(owner, "案件一")
    matched = f"{owner}-案内命中"
    also_in_case = f"{owner}-案内未命中"
    outside = f"{owner}-案外命中"
    # 案件里有 2 个文件，关键词只命中其中 1 个。
    P.bind_resource(case, P.KIND_KNOWLEDGE_DOCUMENT, matched)
    P.bind_resource(case, P.KIND_KNOWLEDGE_DOCUMENT, also_in_case)

    http = _install_http(monkeypatch, [{"id": matched}])
    # 关键词命中 2 个文件：一个在案内、一个在案外。
    await api.list_knowledge_documents_v2(
        _Req(owner),
        doc_ids=f"{matched},{outside}",
        path_id=case,
    )

    # 最后一次请求是真正带过滤条件的列表调用（第一次是解析路径时的文档枚举）。
    filtered = [p for p in http["listing"] if "doc_ids" in p]
    assert filtered, "带路径过滤的列表请求应转发 doc_ids"
    forwarded = sorted(filtered[-1]["doc_ids"].split(","))
    assert forwarded == [matched], (
        f"只应转发案内且命中的文件，实际 {forwarded!r} —— "
        "若为 [also_in_case, matched] 说明 path 覆盖了 keyword"
    )


@requires_platform_db
async def test_foreign_path_returns_an_empty_page(account, monkeypatch):
    """不属于本账号的路径返回空页，而不是退化成「不过滤」。"""
    owner = account
    other = P.create_path(f"{owner}-other", "别人的案件")
    http = _install_http(monkeypatch, [{"id": f"{owner}-a"}])

    result = await api.list_knowledge_documents_v2(
        _Req(owner), path_id=other,
    )

    assert result["items"] == []
    assert result["total"] == 0
    # 解析归属前会枚举调用方自己的文档（安全：列的是自己的），但绝不能发出
    # 带 doc_ids 的过滤请求否则等于放行。
    assert not [p for p in http["listing"] if "doc_ids" in p]


@requires_platform_db
async def test_root_path_passes_every_unfiled_document(account, monkeypatch):
    """根路径 = 公共区：未绑定文件都可见。"""
    owner = account
    root = P.ensure_root_path(owner, "公共")
    filed = f"{owner}-已归入案件"
    case = P.create_path(owner, "案件一")
    P.bind_resource(case, P.KIND_KNOWLEDGE_DOCUMENT, filed)

    http = _install_http(monkeypatch, [{"id": "d1"}, {"id": filed}])
    await api.list_knowledge_documents_v2(_Req(owner), path_id=root)

    filtered = [p for p in http["listing"] if "doc_ids" in p]
    assert filtered, "根路径应转发可见文档集合"
    forwarded = set(filtered[-1]["doc_ids"].split(","))
    assert "d1" in forwarded
    assert filed not in forwarded


@requires_platform_db
async def test_pending_count_survives_switching_to_an_empty_path(account, monkeypatch):
    """切到「没有内容」的路径时，账号级「处理中」计数不能变成 0。

    这是实测报上来的现象：在某个子路径上传文件后切走再切回，状态列上的「待处理」
    就消失了，也看不到后台还在不在跑。根因是列表接口在「该路径没有可见文档」这一
    分支里直接返回 ``pending_count: 0`` —— 那个数字被当成「当前视图的属性」，
    而它其实是**账号级**事实。现在它从独立的计数接口取，与任何筛选无关。
    """
    owner = account
    empty_path = P.create_path(owner, "空路径")
    http = _install_http(monkeypatch, [], pending=3)

    result = await api.list_knowledge_documents_v2(_Req(owner), path_id=empty_path)

    assert result["items"] == [] and result["total"] == 0
    assert result["pending_count"] == 3, "空路径不得把账号级计数归零"
    assert http["pending_calls"], "应通过独立的计数接口取账号级计数"


@requires_platform_db
async def test_pending_count_does_not_leak_documents_of_other_paths(
    account, monkeypatch,
):
    """计数是账号级，但**文档内容**仍严格按路径过滤。

    两件事必须同时成立：计数跨路径常驻，而案件里的文件不能因为计数接口而被顺带
    查出来。这里断言转发给知识服务的 doc_ids 只含本案文件。
    """
    owner = account
    case = P.create_path(owner, "案件一")
    inside = f"{owner}-案内"
    outside = f"{owner}-案外"
    P.bind_resource(case, P.KIND_KNOWLEDGE_DOCUMENT, inside)

    http = _install_http(monkeypatch, [{"id": inside}], pending=1)
    await api.list_knowledge_documents_v2(_Req(owner), path_id=case)

    filtered = [p for p in http["listing"] if "doc_ids" in p]
    assert filtered, "应转发可见文档集合"
    assert outside not in filtered[-1]["doc_ids"]


@requires_platform_db
async def test_deleting_a_document_drops_its_path_binding(account, monkeypatch):
    """删除文档要同时清掉它的路径归属。

    归属行存在平台库，知识服务完全不知道它，所以只转发删除会永久留下孤儿绑定。
    这些行在文档已不存在时不起作用，但绑定表按资源 id 唯一 —— 一旦 id 被复用，
    新文档会静默落进旧文档的案件里。实测就是这样：删掉文档后
    ``resource_bindings`` 里那一行还在。
    """
    owner = account
    case = P.create_path(owner, "案件一")
    document_id = f"{owner}-doc"
    P.bind_resource(case, P.KIND_KNOWLEDGE_DOCUMENT, document_id)
    assert P.path_of_resource(P.KIND_KNOWLEDGE_DOCUMENT, document_id) == case

    _install_http(monkeypatch, [], pending=0)
    # ``_forward`` issues the DELETE; stub it so the test exercises only the
    # binding cleanup that follows a successful delete.
    async def _fake_forward(request, method, path, username):
        return {"deleted": True}

    monkeypatch.setattr(api, "_forward", _fake_forward)
    await api.delete_knowledge_document(document_id, _Req(owner))

    assert P.path_of_resource(P.KIND_KNOWLEDGE_DOCUMENT, document_id) is None


@requires_platform_db
async def test_a_failed_delete_keeps_the_binding(account, monkeypatch):
    """删除失败时不能解除归属 —— 文档还在，路径归属必须保留。

    顺序很关键：先转发删除，成功后才解绑。反过来写的话，一次失败的删除会把仍
    存在的文档从它的案件里摘出去。
    """
    owner = account
    case = P.create_path(owner, "案件一")
    document_id = f"{owner}-doc2"
    P.bind_resource(case, P.KIND_KNOWLEDGE_DOCUMENT, document_id)

    _install_http(monkeypatch, [], pending=0)

    async def _boom(request, method, path, username):
        raise api.HTTPException(status_code=503, detail="service down")

    monkeypatch.setattr(api, "_forward", _boom)
    with pytest.raises(api.HTTPException):
        await api.delete_knowledge_document(document_id, _Req(owner))

    assert P.path_of_resource(P.KIND_KNOWLEDGE_DOCUMENT, document_id) == case


@requires_platform_db
async def test_upload_files_the_document_under_the_given_path(account, monkeypatch):
    """上传时必须按所选路径归入，否则文件一律变成公共。

    这是实测报上来的问题：「在测试1里上传论文扉页.docx，结果跑到公共区了」。原因
    是上传接口**从来没有收到过路径**，前端也没传 —— 界面在某个路径下上传，文件却
    永远落在公共区，之后选中该路径也看不到它。

    这里锁定两件事：文件被归入指定路径；返回体带上 ``path_id`` 以便前端确认落点。
    """
    owner = account
    case = P.create_path(owner, "案件一")
    new_document_id = f"{owner}-uploaded"

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"id": new_document_id, "filename": "x.txt", "status": "queued"}

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            return type("R", (), {"status_code": 200, "raise_for_status": lambda s: None,
                                  "json": lambda s: {"upload_ready": True}})()

        async def post(self, url, **kwargs):
            return _Resp()

    monkeypatch.setattr(
        api, "httpx", types.SimpleNamespace(
            AsyncClient=lambda **kw: _Client(**kw),
            Timeout=lambda *a, **k: None,
            HTTPStatusError=Exception, HTTPError=Exception,
        ),
    )
    monkeypatch.setenv("QWENPAW_KNOWLEDGE_URL", "http://127.0.0.1:18100")

    upload = type("U", (), {"filename": "x.txt", "file": None, "content_type": "text/plain"})()
    result = await api.upload_knowledge_document(_Req(owner), path_id=case, file=upload)

    assert P.path_of_resource(P.KIND_KNOWLEDGE_DOCUMENT, new_document_id) == case
    assert result["path_id"] == case


@requires_platform_db
async def test_upload_rejects_a_foreign_path(account):
    """上传到不属于本账号的路径要拒绝，避免文件散落到别人的案件里。"""
    owner = account
    with pytest.raises(api.HTTPException) as excinfo:
        await api.upload_knowledge_document(
            _Req(owner),
            path_id=str(uuid.uuid4()),
            file=type("U", (), {"filename": "x.txt", "file": None, "content_type": "text/plain"})(),
        )
    assert excinfo.value.status_code == 404
