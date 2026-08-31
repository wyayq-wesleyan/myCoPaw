# 检索来源元数据实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在混合检索 pipeline 中为每个 evidence 添加来源元数据（match_reasons、confidence、source），让 LLM 理解检索路径和可信度。

**Architecture:** 改动分三层：(1) Store 层各检索方法返回时标记来源；(2) `_hybrid_search` 聚合多路命中、归一化置信度；(3) 工具格式化和自动注入展示来源信息。同时准备真实业务测试文档验证效果。

**Tech Stack:** Python 3.11, PostgreSQL 17, psycopg2, numpy, FastAPI, pytest

## Global Constraints

- 知识服务容器: `huizhi-knowledge-api` (app 在 `/opt/huizhi-knowledge/app/`)
- DB: PG17 `huizhi_knowledge`, user `huizhi_knowledge`, password `huizhi_knowledge`
- Docker bind mount: `/tmp/huizhi-v201-test-0725/`
- embedding 不可用时不报错，semantic_enabled=False
- 向后兼容：现有 evidence 字段不变，新增字段为可选

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `knowledge-service/app/store.py` | 各检索方法添加 source 标签 |
| `knowledge-service/app/main.py` | `_hybrid_search` 聚合+置信度, 工具格式化 |
| `src/qwenpaw/agents/tools/knowledge.py` | query_knowledge 输出来源信息 |
| `knowledge-service/tests/test_source_metadata.py` | 单元测试 |
| `knowledge-service/tests/conftest.py` | 测试 fixtures |
| `knowledge-service/tests/test_documents/` | 测试用仿真文档 |
| `docs/superpowers/plans/2026-08-30-retrieval-source-metadata-test-plan.md` | 测试方案 |

---

## Task 1: Store 层各检索方法添加 source 标签

**Files:**
- Modify: `knowledge-service/app/store.py:546-594` (`search`)
- Modify: `knowledge-service/app/store.py:596-660` (`search_by_embedding`)
- Modify: `knowledge-service/app/store.py:684-700` (`entity_context`)

**Interfaces:**
- Produces: 每个返回的 dict 新增 `"source"` 字段（str）

**Changes:**

### Step 1: `search` — 全文 + 字面检索添加 source

在 `store.py` 的 `search` 方法中，所有返回的 item 添加 `"source": "fulltext"` 或 `"source": "literal"`：

```python
# store.py, search() method, after line 594 (return)
# 修改返回逻辑：区分 fulltext 和 literal 来源

def search(self, space_id: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
    with self._connect() as db:
        full_text_rows = db.execute(
            """
            SELECT c.id, c.document_id, c.page, c.location, c.content, d.filename,
                   ts_rank_cd(c.search_vector, plainto_tsquery('simple', %s)) AS score
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE c.space_id = %s
              AND c.search_vector @@ plainto_tsquery('simple', %s)
            ORDER BY score DESC
            LIMIT %s
            """,
            (query, space_id, query, limit),
        ).fetchall()

        terms = self._cjk_bigrams(query)
        if terms:
            whens = " + ".join(
                f"(CASE WHEN c.content LIKE %s THEN 1 ELSE 0 END)" for _ in terms
            )
            like_params = [f"%{term}%" for term in terms]
            rows = db.execute(
                f"""
                SELECT c.id, c.document_id, c.page, c.location, c.content, d.filename,
                       ({whens})::float / %s AS score
                FROM chunks c JOIN documents d ON d.id = c.document_id
                WHERE c.space_id = %s AND ({whens}) > 0
                ORDER BY score DESC
                LIMIT %s
                """,
                [*like_params, float(len(terms)), space_id, *like_params, limit * 3],
            ).fetchall()
        else:
            rows = []

    merged: dict[str, dict[str, Any]] = {}
    for row in full_text_rows:
        item = dict(row)
        item["source"] = "fulltext"
        merged[item["id"]] = item
    for row in rows:
        item = dict(row)
        item["source"] = "literal"
        if item["id"] not in merged or (item.get("score") or 0) > (merged[item["id"]].get("score") or 0):
            merged[item["id"]] = item
    return list(merged.values())[:limit]
```

### Step 2: `search_by_embedding` — 语义检索添加 source

在 `search_by_embedding` 返回前为每个 item 添加 `"source": "semantic"`。在 line ~658 附近，cosine 打分后、return 前：

```python
# 在 search_by_embedding 方法中，cosine 打分后
for item in items:
    item["source"] = "semantic"
return items
```

### Step 3: `entity_context` — 实体检索添加 source

在 `entity_context` 返回前添加 `"source": "entity"`：

```python
def entity_context(self, space_id: str, query: str, limit: int = 50) -> list[dict[str, Any]]:
    with self._connect() as db:
        rows = db.execute(
            """...""",
            (space_id, f"%{query}%", limit),
        ).fetchall()
    results = [dict(row) for row in rows]
    for item in results:
        item["source"] = "entity"
    return results
```

---

## Task 2: `_hybrid_search` 聚合多路命中 + 归一化置信度

**Files:**
- Modify: `knowledge-service/app/main.py:300-336` (`_hybrid_search`)

**Interfaces:**
- Consumes: 各检索方法返回带 `source` 的 item
- Produces: evidence 每项包含 `match_reasons`, `confidence`, `source`

**Changes:**

重写 `_hybrid_search` 的合并逻辑：

```python
async def _hybrid_search(space_id: str, query: str, top_k: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    local_evidence = await asyncio.to_thread(store.entity_context, space_id, query, top_k)
    direct = await asyncio.to_thread(store.search, space_id, query, top_k)
    vector = await asyncio.to_thread(ModelClient(settings).embed, [query])
    semantic = (
        await asyncio.to_thread(
            store.search_by_embedding, space_id, vector[0], top_k, SEMANTIC_MIN_SCORE
        )
        if vector
        else []
    )

    # 聚合多路命中：按 chunk id 收集所有命中来源
    merged: dict[str, dict[str, Any]] = {}
    for item in [*local_evidence, *direct, *semantic]:
        key = item.get("id") or f"{item.get('document_id')}:{item.get('page')}:{item.get('location')}"
        source = item.pop("source", "unknown")
        if key in merged:
            existing = merged[key]
            if source not in existing["match_reasons"]:
                existing["match_reasons"].append(source)
            # 保留较高 score
            existing_score = existing.get("score") or 0
            new_score = item.get("score") or 0
            if new_score > existing_score:
                existing["score"] = new_score
        else:
            item["match_reasons"] = [source]
            merged[key] = item

    # 计算归一化置信度
    SOURCE_WEIGHT = {"fulltext": 1.0, "literal": 0.9, "semantic": 0.8, "entity": 0.7}
    for item in merged.values():
        reasons = item.get("match_reasons", [])
        if len(reasons) >= 3:
            item["confidence"] = "high"
        elif len(reasons) >= 2:
            item["confidence"] = "high"
        else:
            weight = SOURCE_WEIGHT.get(reasons[0], 0.5)
            if weight >= 0.9:
                item["confidence"] = "high"
            elif weight >= 0.7:
                item["confidence"] = "medium"
            else:
                item["confidence"] = "low"
        # 主来源 = 最高权重的路径
        item["source"] = max(reasons, key=lambda r: SOURCE_WEIGHT.get(r, 0))

    # LightRAG 图检索
    graph_results = []
    if merged:
        graph_results = await lightrag.search(query, space_id, top_k)
    if graph_results:
        for g in graph_results:
            g["match_reasons"] = [g.get("source", "lightrag")]
            g["confidence"] = "medium"
        reserved = min(len(graph_results), max(2, top_k // 3))
        local_items = list(merged.values())
        evidence = [*local_items[:max(0, top_k - reserved)], *graph_results[:reserved]]
    else:
        evidence = list(merged.values())[:top_k]

    relations = await asyncio.to_thread(store.relation_context, space_id, query, top_k)
    return evidence, relations, bool(vector)
```

---

## Task 3: `/v1/search` 响应包含来源元数据

**Files:**
- Modify: `knowledge-service/app/main.py:402-443` (`search` endpoint)

**Changes:**

在 `/v1/search` 中，evidence 已经包含 `match_reasons`, `confidence`, `source`，只需确保响应格式化时展示。修改 doc_groups 的来源统计：

```python
@app.post("/v1/search", dependencies=[Depends(require_api_key)])
async def search(body: SearchRequest, scope: tuple[str, bool] = Depends(request_scope)) -> dict[str, Any]:
    # ... 前面不变 ...
    evidence, relations, semantic_enabled = await _hybrid_search(
        body.space_id, body.query, body.top_k
    )
    relations = relations if body.include_relations else []

    if body.min_score > 0:
        evidence = [item for item in evidence if (item.get("score") or 0) >= body.min_score]

    # 按文档分组统计，含来源信息
    doc_groups: dict[str, dict[str, Any]] = {}
    for item in evidence:
        doc_id = item.get("document_id") or "lightrag-graph"
        if doc_id not in doc_groups:
            doc_groups[doc_id] = {
                "document_id": doc_id,
                "filename": item.get("filename") or ("LightRAG 图检索" if doc_id == "lightrag-graph" else None),
                "snippet_count": 0,
                "pages": [],
                "sources": [],
            }
        doc_groups[doc_id]["snippet_count"] += 1
        page = item.get("page")
        if page and page not in doc_groups[doc_id]["pages"]:
            doc_groups[doc_id]["pages"].append(page)
        source = item.get("source", "unknown")
        if source not in doc_groups[doc_id]["sources"]:
            doc_groups[doc_id]["sources"].append(source)

    return {
        "query": body.query,
        "evidence": evidence,
        "documents": list(doc_groups.values()),
        "relations": relations,
        "total_count": len(evidence),
        "document_count": len(doc_groups),
        "semantic_search_enabled": semantic_enabled,
    }
```

---

## Task 4: `query_knowledge` 工具格式化来源信息

**Files:**
- Modify: `src/qwenpaw/agents/tools/knowledge.py:45-71` (`_format_search_result`)
- Modify: `src/qwenpaw/agents/tools/knowledge.py:74-138` (`query_knowledge` tool description)

**Changes:**

更新 `_format_search_result` 展示来源和置信度：

```python
def _format_search_result(payload: dict[str, Any]) -> str:
    """格式化检索结果，包含来源和置信度信息"""
    documents = payload.get("documents") or []
    evidence = payload.get("evidence") or []
    total_count = payload.get("total_count", 0)
    document_count = payload.get("document_count", 0)

    if not documents:
        return "未找到相关文档。"

    SOURCE_LABEL = {
        "fulltext": "全文检索",
        "literal": "关键词匹配",
        "semantic": "语义相似",
        "entity": "实体匹配",
        "lightrag": "知识图谱",
    }
    CONFIDENCE_LABEL = {
        "high": "高置信",
        "medium": "中置信",
        "low": "低置信",
    }

    lines = [f"找到 {document_count} 个相关文档，共 {total_count} 个相关片段：\n"]

    for i, doc in enumerate(documents[:5], 1):
        doc_id = doc.get("document_id", "")
        filename = doc.get("filename", "未知文件")
        snippet_count = doc.get("snippet_count", 0)
        pages = doc.get("pages", [])
        sources = doc.get("sources", [])
        pages_str = f"，涉及页码：{', '.join(str(p) for p in sorted(pages)[:5])}" if pages else ""
        sources_str = "，来源：" + "、".join(SOURCE_LABEL.get(s, s) for s in sources) if sources else ""
        lines.append(f"[{i}] {filename}（{snippet_count} 个相关片段{pages_str}{sources_str}）")
        lines.append(f"    文档ID: {doc_id}")

    if document_count > 5:
        lines.append(f"\n... 还有 {document_count - 5} 个文档未显示")

    lines.append("\n提示：使用 get_document_detail 工具查看特定文档的详细内容。")

    return "\n".join(lines)
```

更新工具描述，增加来源说明：

```python
@tool_descriptor(
    name="query_knowledge",
    # ... 其他参数不变 ...
    ui_description="Search the current account's local knowledge spaces",
)
async def query_knowledge(query: str, space_name: str = "") -> ToolChunk:
    """Search local knowledge before answering questions grounded in uploaded materials.

    检索来源说明：
    - 全文检索(fulltext)：直接匹配原文，可信度最高
    - 关键词匹配(literal)：CJK 二元组部分匹配，可信度高
    - 语义相似(semantic)：embedding 向量相似，可能不完全精确
    - 实体匹配(entity)：人名/机构名等实体匹配
    - 知识图谱(lightrag)：图谱关系推断，需核实原文

    回答时优先使用"全文检索"和"关键词匹配"来源的内容，对"知识图谱"
    来源的推断性内容需谨慎使用，尽量引用原文佐证。
    """
```

---

## Task 5: 自动注入包含来源信息

**Files:**
- Modify: `src/qwenpaw/agents/react_agent.py:1017-1039` (`_inject_auto_knowledge_context`)

**Changes:**

修改自动注入的 snippets 格式化，加入来源标签：

```python
# 在 _inject_auto_knowledge_context 中，snippets.append 部分
SOURCE_LABEL = {
    "fulltext": "全文检索",
    "literal": "关键词匹配",
    "semantic": "语义相似",
    "entity": "实体匹配",
    "lightrag": "知识图谱",
}

for item in (payload.get("evidence") or [])[:6]:
    source = SOURCE_LABEL.get(item.get("source", ""), item.get("source", ""))
    confidence = item.get("confidence", "")
    confidence_str = f" [{confidence}置信]" if confidence else ""
    snippets.append(
        f"来源：{item.get('filename') or '未知文件'}，第 {item.get('page') or '?'} 页"
        f"（{source}{confidence_str}）\n"
        f"{str(item.get('content') or '')[:900]}"
    )
```

---

## Task 6: 准备测试用仿真业务文档

**Files:**
- Create: `knowledge-service/tests/test_documents/公司信息安全管理制度.md`
- Create: `knowledge-service/tests/test_documents/2024年年度审计报告.txt`
- Create: `knowledge-service/tests/test_documents/项目管理规范手册.md`

**要求：**
- 每个文档 3000-8000 字，中文
- 内容包含可检索的实体名、专有名词、数据
- 三篇文档之间有交叉引用关系（如"张三"出现在多篇文档中）
- 覆盖不同检索路径的测试场景

**文档1：公司信息安全管理制度.md**
- 包含制度条款编号、具体措施
- 人名实体："张三"（CISO）、"李四"（安全总监）
- 专有名词："零信任架构"、"SOC2"、"ISO27001"
- 数字数据：罚款金额、审计周期

**文档2：2024年年度审计报告.txt**
- 审计发现、风险等级
- 人名实体："王五"（审计组长）、"张三"（配合部门）
- 专有名词："IT一般控制"、"数据完整性"、"合规性审计"
- 数字数据：审计发现数、整改完成率

**文档3：项目管理规范手册.md**
- 项目流程、角色职责
- 人名实体："赵六"（PMO负责人）
- 专有名词："敏捷开发"、"PRINCE2"、"关键路径法"
- 与前两篇有交叉：提到了"信息安全评审"和"审计合规"

---

## Task 7: 上传测试文档到知识服务

**Commands:**

```bash
# 1. 确保知识服务容器运行
docker ps --filter "name=huizhi-knowledge-api" --format "{{.Status}}"

# 2. 上传三篇测试文档
for doc in 公司信息安全管理制度.md 2024年年度审计报告.txt 项目管理规范手册.md; do
  curl -s -X POST http://localhost:18100/v1/documents/upload \
    -H "X-Knowledge-Key: huizhi-local-knowledge-dev" \
    -H "X-User-Id: admin" \
    -H "X-Is-Admin: true" \
    -F "file=@knowledge-service/tests/test_documents/$doc" | python3 -m json.tool
done

# 3. 等待处理完成（轮询 jobs）
watch -n 5 'curl -s -H "X-Knowledge-Key: huizhi-local-knowledge-dev" -H "X-User-Id: admin" -H "X-Is-Admin: true" http://localhost:18100/v1/jobs | python3 -c "import sys,json; [print(j[\"filename\"],j[\"status\"]) for j in json.load(sys.stdin)]"'
```

---

## Task 8: 测试方案设计

**Files:**
- Create: `docs/superpowers/plans/2026-08-30-retrieval-source-metadata-test-plan.md`

---

## Task 9: 集成测试验证

**Commands:**

```bash
# 测试1：全文检索命中（"零信任架构" 只在文档1出现）
curl -s -X POST http://localhost:18100/v1/search \
  -H "X-Knowledge-Key: huizhi-local-knowledge-dev" \
  -H "X-User-Id: admin" -H "X-Is-Admin: true" \
  -d '{"space_id":"<space_id>","query":"零信任架构","top_k":5}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); [print(e.get('source'),e.get('confidence'),e.get('match_reasons'),e.get('content','')[:60]) for e in d['evidence']]"

# 测试2：多路命中（"张三" 出现在文档1和2）
curl -s -X POST http://localhost:18100/v1/search \
  -H "X-Knowledge-Key: huizhi-local-knowledge-dev" \
  -H "X-User-Id: admin" -H "X-Is-Admin: true" \
  -d '{"space_id":"<space_id>","query":"张三","top_k":10}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); [print(e.get('source'),e.get('confidence'),e.get('match_reasons'),e.get('content','')[:60]) for e in d['evidence']]"

# 测试3：语义检索（"网络安全风险评估" 同义表述）
curl -s -X POST http://localhost:18100/v1/search \
  -H "X-Knowledge-Key: huizhi-local-knowledge-dev" \
  -H "X-User-Id: admin" -H "X-Is-Admin: true" \
  -d '{"space_id":"<space_id>","query":"网络安全风险评估","top_k":5}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); [print(e.get('source'),e.get('confidence'),e.get('match_reasons'),e.get('content','')[:60]) for e in d['evidence']]"

# 测试4：实体匹配（"王五" 只在文档2出现）
curl -s -X POST http://localhost:18100/v1/search \
  -H "X-Knowledge-Key: huizhi-local-knowledge-dev" \
  -H "X-User-Id: admin" -H "X-Is-Admin: true" \
  -d '{"space_id":"<space_id>","query":"王五","top_k":5}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); [print(e.get('source'),e.get('confidence'),e.get('match_reasons'),e.get('content','')[:60]) for e in d['evidence']]"

# 测试5：工具调用格式（通过 main app proxy）
TOKEN=$(curl -s -X POST http://localhost:18088/api/auth/login -H 'Content-Type: application/json' -d '{"username":"admin","password":"Admin@123456"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:18088/api/knowledge/search/filter \
  -H "Content-Type: application/json" \
  -d '{"query":"信息安全审计","top_k":10}' | python3 -m json.tool
```

---

## Task 10: 清理与验证

**Commands:**

```bash
# 验证所有容器正常运行
docker ps --filter "name=huizhi" --format "{{.Names}} {{.Status}}"

# 验证前端正常
curl -s http://localhost:18088/ | head -3

# 验证知识服务健康
curl -s http://localhost:18100/health | python3 -m json.tool
```
