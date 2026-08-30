# 知识空间重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 移除空间概念，所有知识文档按用户隔离。页面重构为"上查询、下列表"布局。新增上传队列和部分预览。

**Architecture:** 后端使用隐式单空间（每个用户自动对应 `user_{username}` 默认空间），前端隐藏空间概念。新增预览接口、上传队列抽屉、检索筛选。

**Tech Stack:** 现有 knowledge-service（FastAPI, PG17, psycopg2），前端 React + Ant Design。

## Global Constraints

- 后端不删除 `spaces` 表，保留现有数据模型
- 每个用户自动获取/创建默认空间 `user_{username}`
- 单文件上传限制 < 10MB
- 预览为部分预览（前 N 条，N=100）
- 检索问答仅作筛选条件，不展示片段
- 不破坏现有文档/知识检索功能

---

## Task 1: Backend Store — 隐式用户空间

**Files:**
- Modify: `knowledge-service/app/store.py`

**Interfaces:**
- Produces: `get_or_create_user_space(conn, owner_id)` → returns space dict
- Modifies: `list_documents` 增加按 owner_id 过滤（可选）
- Modifies: `create_document` 可选接收 space_id（如不传则用默认空间）

- [ ] **Step 1: 在 store.py 中新增 get_or_create_user_space**

```python
# knowledge-service/app/store.py — 新增函数
import psycopg2

def get_or_create_user_space(owner_id: str) -> dict:
    """获取或创建用户的默认空间（user_{owner_id}）。"""
    space_name = f"user_{owner_id}"
    with _pool.getconn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, owner_id, created_at FROM spaces WHERE owner_id = %s LIMIT 1", (owner_id,))
                row = cur.fetchone()
                if row:
                    return {"id": row[0], "name": row[1], "owner_id": row[2], "created_at": str(row[3])}
                import uuid
                space_id = str(uuid.uuid4())
                cur.execute("INSERT INTO spaces (id, name, owner_id) VALUES (%s, %s, %s) RETURNING id, name, owner_id, created_at",
                            (space_id, space_name, owner_id))
                row = cur.fetchone()
                conn.commit()
                return {"id": row[0], "name": row[1], "owner_id": row[2], "created_at": str(row[3])}
        finally:
            _pool.putconn(conn)
```

- [ ] **Step 2: 新增 list_user_documents 函数（按 owner_id 聚合所有文档）**

```python
def list_user_documents(owner_id: str, is_admin: bool = False,
                        filename_like: str = None, created_after: str = None, created_before: str = None,
                        doc_ids: list[str] = None) -> list[dict]:
    """列出该用户所有文档（跨所有空间），可选文件名模糊、时间、指定文档ID过滤。"""
    # SQL 动态拼接条件
    conditions = []
    params = []
    if not is_admin:
        conditions.append("d.owner_id = %s")
        params.append(owner_id)
    if filename_like:
        conditions.append("d.filename ILIKE %s")
        params.append(f"%{filename_like}%")
    if created_after:
        conditions.append("d.created_at >= %s")
        params.append(created_after)
    if created_before:
        conditions.append("d.created_at <= %s")
        params.append(created_before)
    if doc_ids:
        placeholders = ",".join(["%s"] * len(doc_ids))
        conditions.append(f"d.id IN ({placeholders})")
        params.extend(doc_ids)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT d.id, d.filename, d.status, d.error, d.created_at, d.updated_at,
               j.id as job_id, j.status as job_status, j.attempts, j.error as job_error
        FROM documents d
        LEFT JOIN LATERAL (
            SELECT id, status, attempts, error FROM jobs
            WHERE document_id = d.id ORDER BY created_at DESC LIMIT 1
        ) j ON true
        {where}
        ORDER BY d.created_at DESC
    """
    with _pool.getconn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                return [{"id": r[0], "filename": r[1], "status": r[2], "error": r[3],
                         "created_at": str(r[4]) if r[4] else None,
                         "updated_at": str(r[5]) if r[5] else None,
                         "job_id": r[6]} for r in rows]
        finally:
            _pool.putconn(conn)
```

- [ ] **Step 3: 新增 list_user_jobs 函数**

```python
def list_user_jobs(owner_id: str, is_admin: bool = False) -> list[dict]:
    """列出该用户所有 job 状态。"""
    conditions = []
    params = []
    if not is_admin:
        conditions.append("j.owner_id = %s")
        params.append(owner_id)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT j.id, j.document_id, j.status, j.attempts, j.error, j.created_at, j.updated_at,
               d.filename
        FROM jobs j
        JOIN documents d ON d.id = j.document_id
        {where}
        ORDER BY j.created_at DESC
    """
    with _pool.getconn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                return [{"id": r[0], "document_id": r[1], "status": r[2], "attempts": r[3],
                         "error": r[4], "created_at": str(r[5]) if r[5] else None,
                         "updated_at": str(r[6]) if r[6] else None,
                         "filename": r[7]} for r in rows]
        finally:
            _pool.putconn(conn)
```

- [ ] **Step 4: 验证 store 函数可正常导入**

```bash
docker exec huizhi-v210-check python3 -c "
from knowledge_service.app.store import get_or_create_user_space, list_user_documents, list_user_jobs
print('Store functions OK')
"
```

- [ ] **Step 5: Commit**

---

## Task 2: Backend API — 隐式空间解析

**Files:**
- Modify: `knowledge-service/app/main.py`

**Interfaces:**
- Modifies: `upload_document` 接受 `space_id` 可选，缺失时用 `get_or_create_user_space` 解析
- 新增: `GET /v1/documents` — 按用户列出文档（支持文件名/时间/文档ID过滤）
- 新增: `GET /v1/jobs` — 按用户列出任务
- 新增: `GET /v1/documents/{id}/preview` — 部分预览（前100条内容）
- 新增: `POST /v1/search/filter` — 检索筛选（返回命中的 document_id 集合）

- [ ] **Step 1: 修改 upload_document — space_id 可选**

在 main.py 中修改 `upload_document` 函数签名，将 `space_id: str = Form(...)` 改为 `space_id: str = Form(default="")`，如果为空则调用 `get_or_create_user_space(owner_id)` 获取默认空间。

```python
@app.post("/v1/documents/upload", dependencies=[Depends(require_api_key)])
async def upload_document(
    file: UploadFile = File(...),
    space_id: str = Form(default=""),
    scope: tuple[str, bool] = Depends(request_scope),
) -> dict[str, Any]:
    owner_id, is_admin = scope
    if not space_id:
        space_obj = await asyncio.to_thread(get_or_create_user_space, owner_id)
        space_id = space_obj["id"]
    # ... 后续逻辑不变
```

- [ ] **Step 2: 新增 GET /v1/documents 列表接口**

```python
@app.get("/v1/documents", dependencies=[Depends(require_api_key)])
async def list_all_documents(
    filename: str = Query(default=""),
    created_after: str = Query(default=""),
    created_before: str = Query(default=""),
    doc_ids: str = Query(default=""),
    scope: tuple[str, bool] = Depends(request_scope),
) -> list[dict[str, Any]]:
    owner_id, is_admin = scope
    ids = [x.strip() for x in doc_ids.split(",") if x.strip()] if doc_ids else None
    return await asyncio.to_thread(
        store.list_user_documents, owner_id, is_admin,
        filename_like=filename or None,
        created_after=created_after or None,
        created_before=created_before or None,
        doc_ids=ids,
    )
```

- [ ] **Step 3: 新增 GET /v1/jobs 列表接口**

```python
@app.get("/v1/jobs", dependencies=[Depends(require_api_key)])
async def list_all_jobs(
    scope: tuple[str, bool] = Depends(request_scope),
) -> list[dict[str, Any]]:
    owner_id, is_admin = scope
    return await asyncio.to_thread(store.list_user_jobs, owner_id, is_admin)
```

- [ ] **Step 4: 新增 GET /v1/documents/{id}/preview 接口**

读取文件开头内容（前100行/条），按格式返回：

```python
@app.get("/v1/documents/{document_id}/preview", dependencies=[Depends(require_api_key)])
async def preview_document(document_id: str, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    owner_id, is_admin = scope
    document = await asyncio.to_thread(store.get_document, document_id, owner_id, is_admin)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    file_path = Path(document["path"])
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")
    suffix = file_path.suffix.lower()
    MAX_LINES = 100
    content = ""
    if suffix in (".txt", ".md", ".json", ".csv"):
        content = file_path.read_text(encoding="utf-8", errors="replace")[:MAX_LINES * 200]
    elif suffix in (".xls", ".xlsx", ".xlsm"):
        # 用 openpyxl/pandas 读取前 N 行
        try:
            import pandas as pd
            df = pd.read_excel(str(file_path), nrows=MAX_LINES)
            content = df.to_string(index=False)
        except Exception as e:
            content = f"[Excel预览失败: {e}]"
    elif suffix == ".pdf":
        content = f"[PDF预览：请使用原文件查看，或通过 chunks 接口查看内容片段]"
    elif suffix in (".doc", ".docx"):
        content = f"[DOCX预览：请使用原文件查看，或通过 chunks 接口查看内容片段]"
    else:
        content = file_path.read_text(encoding="utf-8", errors="replace")[:MAX_LINES * 200]
    return {"document_id": document_id, "filename": document["filename"], "content": content, "truncated": True}
```

- [ ] **Step 5: 新增 POST /v1/search/filter 接口**

调用 search，仅返回命中的 document_id 集合：

```python
@app.post("/v1/search/filter", dependencies=[Depends(require_api_key)])
async def search_filter(
    body: SearchRequest,
    scope: tuple[str, bool] = Depends(request_scope),
) -> dict[str, Any]:
    owner_id, is_admin = scope
    space_obj = await asyncio.to_thread(get_or_create_user_space, owner_id)
    space_id = space_obj["id"]
    evidence, relations, semantic = await _hybrid_search(space_id, body.query, body.top_k or 20)
    doc_ids = list({e.get("document_id") for e in evidence if e.get("document_id")})
    return {"document_ids": doc_ids, "total_count": len(evidence), "document_count": len(doc_ids)}
```

- [ ] **Step 6: 验证所有接口可正常导入**

```bash
docker exec huizhi-v210-check python3 -c "
from knowledge_service.app.main import app
print('FastAPI app OK')
print([r.path for r in app.routes if hasattr(r, 'path')])
"
```

- [ ] **Step 7: Commit**

---

## Task 3: Backend — 历史数据迁移脚本

**Files:**
- Create: `knowledge-service/scripts/merge_spaces.py`

- [ ] **Step 1: 创建迁移脚本**

```python
#!/usr/bin/env python3
"""把每个用户所有历史空间文档合并到该用户的默认空间。"""
import psycopg2
import uuid
import os

DATABASE_URL = os.environ.get("KNOWLEDGE_DATABASE_URL", "postgresql://huizhi_knowledge:huizhi_knowledge@host.docker.internal:15432/huizhi_knowledge")

def migrate():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    # 找到所有有文档的用户
    cur.execute("""
        SELECT DISTINCT owner_id FROM documents
    """)
    owners = [r[0] for r in cur.fetchall()]
    print(f"Found {len(owners)} owners with documents")
    for owner_id in owners:
        # 获取或创建默认空间
        default_name = f"user_{owner_id}"
        cur.execute("SELECT id FROM spaces WHERE owner_id = %s AND name = %s", (owner_id, default_name))
        row = cur.fetchone()
        if row:
            default_space_id = row[0]
        else:
            default_space_id = str(uuid.uuid4())
            cur.execute("INSERT INTO spaces (id, name, owner_id) VALUES (%s, %s, %s)",
                        (default_space_id, default_name, owner_id))
        # 把该用户所有其他空间的文档合并到默认空间
        cur.execute("""
            SELECT id FROM spaces WHERE owner_id = %s AND id != %s
        """, (owner_id, default_space_id))
        other_space_ids = [r[0] for r in cur.fetchall()]
        if other_space_ids:
            placeholders = ",".join(["%s"] * len(other_space_ids))
            cur.execute(f"""
                UPDATE documents SET space_id = %s
                WHERE space_id IN ({placeholders})
            """, [default_space_id] + other_space_ids)
            cur.execute(f"""
                UPDATE chunks SET space_id = %s
                WHERE space_id IN ({placeholders})
            """, [default_space_id] + other_space_ids)
            # 删除空旧空间
            cur.execute(f"DELETE FROM spaces WHERE id IN ({placeholders})", other_space_ids)
            print(f"  Owner {owner_id}: merged {len(other_space_ids)} spaces into {default_space_id}")
        else:
            print(f"  Owner {owner_id}: already has single space {default_space_id}")
    conn.commit()
    cur.close()
    conn.close()
    print("Migration complete")

if __name__ == "__main__":
    migrate()
```

- [ ] **Step 2: 在容器内运行迁移**

```bash
docker cp knowledge-service/scripts/merge_spaces.py huizhi-v210-check:/workspace/merge_spaces.py
docker exec huizhi-v210-check python3 /workspace/merge_spaces.py
```

- [ ] **Step 3: Commit**

---

## Task 4: Frontend API Module — 移除 space_id

**Files:**
- Modify: `console/src/api/modules/knowledge.ts`

**Interfaces:**
- Produces: `listDocuments()` 不传 space_id，支持 filename/time/docIds 过滤
- Produces: `upload(file)` 不传 space_id
- Produces: `searchFilter(query)` 返回 document_ids
- Produces: `previewDocument(id)` 获取部分预览
- Produces: `listJobs()` 获取上传队列
- 移除: `listSpaces`, `createSpace`（或保留为空函数）

- [ ] **Step 1: 更新 knowledge.ts**

移除 `listSpaces`/`createSpace`，新增/修改以下接口：

```typescript
// console/src/api/modules/knowledge.ts
export interface KnowledgeDocumentPreview {
  document_id: string;
  filename: string;
  content: string;
  truncated: boolean;
}

export interface KnowledgeSearchFilterResult {
  document_ids: string[];
  total_count: number;
  document_count: number;
}

export interface KnowledgeJobItem {
  id: string;
  document_id: string;
  status: string;
  attempts: number;
  error?: string | null;
  created_at?: string;
  updated_at?: string;
  filename: string;
}

export const knowledgeApi = {
  health: () => request<{ configured: boolean; status: string }>("/knowledge/health"),
  // listSpaces / createSpace — 移除或保留为空
  listDocuments: (params?: { filename?: string; created_after?: string; created_before?: string; doc_ids?: string }) => {
    const query = new URLSearchParams();
    if (params?.filename) query.set("filename", params.filename);
    if (params?.created_after) query.set("created_after", params.created_after);
    if (params?.created_before) query.set("created_before", params.created_before);
    if (params?.doc_ids) query.set("doc_ids", params.doc_ids);
    const qs = query.toString();
    return request<KnowledgeDocument[]>(`/knowledge/documents${qs ? "?" + qs : ""}`);
  },
  reprocessDocument: (documentId: string) =>
    request<KnowledgeDocument>(`/knowledge/documents/${encodeURIComponent(documentId)}/reprocess`, { method: "POST" }),
  searchFilter: (query: string) =>
    request<KnowledgeSearchFilterResult>("/knowledge/search/filter", {
      method: "POST",
      body: JSON.stringify({ query, top_k: 50 }),
    }),
  previewDocument: (documentId: string) =>
    request<KnowledgeDocumentPreview>(`/knowledge/documents/${encodeURIComponent(documentId)}/preview`),
  listJobs: () => request<KnowledgeJobItem[]>("/knowledge/jobs"),
  upload: async (file: File): Promise<KnowledgeDocument> => {
    const form = new FormData();
    // space_id 由后端自动解析，前端不传
    form.append("file", file);
    const response = await fetch(getApiUrl("/knowledge/documents/upload"), {
      method: "POST",
      headers: buildAuthHeaders(),
      body: form,
    });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  },
  getFileUrl: (documentId: string) => getApiUrl(`/knowledge/documents/${encodeURIComponent(documentId)}/file`),
  getFileHeaders: () => buildAuthHeaders(),
};
```

- [ ] **Step 2: 验证 TypeScript 编译通过**

```bash
cd console && npx tsc --noEmit
```

- [ ] **Step 3: Commit**

---

## Task 5: Frontend Knowledge Page — 新布局

**Files:**
- Modify: `console/src/pages/Knowledge/index.tsx`
- Modify: `console/src/pages/Knowledge/index.module.less`

**Interfaces:**
- Produces: 上半部分查询条件区（上传时间、文件名、检索问答关键词、上传按钮）
- Produces: 下半部分文件列表
- Produces: 上传队列抽屉
- Produces: 文件点击 → 原文预览 Drawer
- 保留: 检索问答搜索卡片（可选保留或移至查询条件）

- [ ] **Step 1: 重构页面布局为上下分区**

移除左侧空间选择器，改为：
- 顶部操作栏：上传按钮 + 上传队列按钮
- 查询条件区：Row 内含 RangePicker（上传时间）、Input（文件名）、Input（检索问答）、查询/重置按钮
- 文件列表 Table：文件名（点击查看预览）、状态、上传时间、操作

- [ ] **Step 2: 实现查询逻辑**

三种条件可组合生效（AND）：
- 上传时间：RangePicker → created_after/created_before
- 文件名：Input → filename 参数
- 检索问答：调 searchFilter → doc_ids 过滤

- [ ] **Step 3: 实现上传队列 Drawer**

Drawer 内展示 jobs 列表 Table，状态列带 Tag 颜色，支持定时轮询。

- [ ] **Step 4: 实现文件预览 Drawer**

点击文件行 → 调 previewDocument → 按文件类型渲染内容。

- [ ] **Step 5: 上传文件大小校验**

上传前校验 `file.size < 10 * 1024 * 1024`，超限提示。

- [ ] **Step 6: 构建前端**

```bash
cd console && npm run build
```

- [ ] **Step 7: Commit**

---

## Task 6: Deploy & E2E Verify

**Files:**
- None (deployment and verification)

- [ ] **Step 1: 运行迁移脚本**

```bash
docker cp knowledge-service/scripts/merge_spaces.py huizhi-v210-check:/workspace/merge_spaces.py
docker exec huizhi-v210-check python3 /workspace/merge_spaces.py
```

- [ ] **Step 2: 部署后端 + 前端**

```bash
docker cp src/qwenpaw/app/routers/knowledge.py huizhi-v210-check:/workspace/app/src/qwenpaw/app/routers/knowledge.py
docker cp knowledge-service/app/main.py huizhi-knowledge-worker:/workspace/app/main.py
docker cp knowledge-service/app/store.py huizhi-knowledge-worker:/workspace/app/store.py
docker cp console/dist/. huizhi-v210-check:/workspace/app/console/dist/
docker restart huizhi-v210-check
docker restart huizhi-knowledge-worker
```

- [ ] **Step 3: E2E 测试 — 创建用户默认空间**

```bash
curl -s -X POST "http://localhost:18088/knowledge/documents/upload" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@test.txt" | python3 -m json.tool
```

- [ ] **Step 4: E2E 测试 — 列出文档（无 space_id）**

```bash
curl -s "http://localhost:18088/knowledge/documents" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

- [ ] **Step 5: E2E 测试 — 部分预览**

```bash
curl -s "http://localhost:18088/knowledge/documents/$DOC_ID/preview" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

- [ ] **Step 6: E2E 测试 — 上传队列**

```bash
curl -s "http://localhost:18088/knowledge/jobs" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

- [ ] **Step 7: E2E 测试 — 检索筛选**

```bash
curl -s -X POST "http://localhost:18088/knowledge/search/filter" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "订单","top_k": 20}' | python3 -m json.tool
```

- [ ] **Step 8: 前端验证 — 浏览器打开知识空间页面**

- [ ] **Step 9: Commit（如有额外修改）**
