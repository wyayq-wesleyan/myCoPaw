# 知识文档级联删除孤儿清理方案

## 概述

修复文档删除后的 5 个孤儿/清理缺口：entities 残留、LightRAG 图谱残留、并发竞态、relations 幽灵行、文件系统孤儿。

## 修复范围

| # | 问题 | 严重度 | 修复方式 |
|---|------|--------|----------|
| 1 | entities 表不清理 | CRITICAL | 删除文档后清理零-mention 实体 |
| 3 | LightRAG 图谱不清理 | CRITICAL | 调用 LightRAG `DELETE /documents/delete_document` |
| 4 | 并发删除竞态 | HIGH | 删除前检查文档状态，非 completed 则拒绝或标记 |
| 5 | relations 残留 | MEDIUM | 补充清理 NULL-evidence relations |
| 7a | 文件先存后写 DB | MEDIUM | 调整上传顺序，先写 DB 再存文件 |

**不修**：空间删除（#2，涉及 UI）、reindex 重复（#11）、审计日志（#10）等。

## 方案设计

### 1. entities 表清理

**当前**：`delete_document()` 删除 relations/mentions/chunks/jobs/document，但不碰 entities。

**修改**：在 `delete_document()` 中，删除 mentions 后，添加一步：

```python
# 清理孤立实体：该空间内、无任何 mention 引用的实体
db.execute("""
    DELETE FROM entities
    WHERE id IN (
        SELECT e.id FROM entities e
        WHERE e.space_id = %s
        AND NOT EXISTS (
            SELECT 1 FROM mentions m
            JOIN chunks c ON c.id = m.chunk_id
            WHERE m.entity_id = e.id
        )
    )
""", (document["space_id"],))
```

**关键点**：
- 按 space_id 过滤，避免误删其他空间的同名实体
- 子查询 JOIN chunks 确保只清理无任何 mention 的实体
- 放在 delete mentions/chunks 之后执行（此时该文档的 mentions 已删）

### 2. LightRAG 图谱清理

**当前**：LightRAG 无 delete 调用，图谱数据永久残留。

**修改**：在 `delete_document()` 中，文档删除后异步调用 LightRAG delete：

```python
# store.delete_document() 返回后，main.py 的 delete_document 端点中
# 异步调用 LightRAG 清理
lightrag_bridge = LightRAGBridge(settings)
await lightrag_bridge.delete_document(document_id)
```

**LightRAGBridge 新增方法**：

```python
async def delete_document(self, document_id: str) -> bool:
    """删除 LightRAG 中指定文档的所有数据"""
    if not self.enabled:
        return False
    headers = {"X-API-Key": self.settings.lightrag_api_key} if self.settings.lightrag_api_key else {}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.delete(
                f"{self.settings.lightrag_url}/documents/delete_document",
                headers=headers,
                json={"doc_ids": [document_id], "delete_llm_cache": True},
            )
            if response.status_code == 409:  # busy, already being deleted
                return True
            response.raise_for_status()
            return True
    except Exception:
        return False  # best-effort, log warning
```

**关键点**：
- LightRAG 的 `file_source` 格式是 `knowledge://{space_id}/{document_id}/{source_name}`
- 但 LightRAG 的 delete API 接受 `doc_ids`（LightRAG 自己的文档 ID）
- **问题**：我们用 `POST /documents/text` 插入时，LightRAG 会为每个调用生成自己的 doc_id，但我们没有保存这个 ID
- **解决方案**：改用 LightRAG 的 `file_source` 作为过滤条件，或在插入时保存返回的 doc_id

**实际方案**：检查 LightRAG 的 `POST /documents/text` 返回值，保存 doc_id：

```python
async def insert(self, *, space_id, document_id, source_name, chunks) -> str | None:
    """返回 LightRAG doc_id 或 None"""
    # ... existing code ...
    response = await client.post(...)
    if response.status_code in (200, 201):
        data = response.json()
        return data.get("id") or data.get("doc_id")  # 保存返回的 ID
    return None
```

然后在 processing.py 中保存 doc_id 到 document 表（新增 `lightrag_doc_id` 字段），或直接用 `file_source` 前缀匹配删除。

**更简单的方案**：不保存 doc_id，改用 `GET /documents` 列表 + 按 `file_source` 过滤删除：

```python
async def delete_document_by_source(self, space_id: str, document_id: str) -> bool:
    """按 file_source 前缀删除 LightRAG 文档"""
    # 先 GET /documents 找到匹配的 doc_ids
    # 再 DELETE /documents/delete_document 删除
```

**最终选择**：在 `delete_document` 的 main.py 端点中，直接传 `document_id` 给 LightRAG 的 `doc_ids` 参数。因为 LightRAG 在 `POST /documents/text` 时会以 `file_source` 作为文档标识，我们需要确认 LightRAG 实际使用的 doc_id 格式。先用 `GET /documents` 确认。

### 3. 并发删除竞态防护

**当前**：worker 处理中（status=processing）删除文档，可能导致 `replace_document_content()` 写入孤儿数据。

**修改**：在 `delete_document()` 中，检查文档状态：

```python
def delete_document(self, document_id, owner_id, is_admin=False):
    document = self.get_document(document_id, owner_id, is_admin)
    if not document:
        return False
    
    # 如果文档正在处理中，先标记为删除中，让 worker 知道
    if document.get("status") == "processing":
        # 方案A：拒绝删除，提示稍后重试
        return False
        # 方案B：标记删除标记，让 worker 完成后检查
        # db.execute("UPDATE documents SET status = 'deleting' WHERE id = %s", (document_id,))
    
    # ... existing deletion logic ...
```

**选择方案 A**（拒绝删除）：简单可靠，避免复杂的竞态协调。用户看到文档在处理中时，提示"文档正在处理，请稍后重试"。

### 4. relations 幽灵行清理

**当前**：`evidence_chunk_id` 被 SET NULL 后，relation 行保留但无实际意义。

**修改**：在 `delete_document()` 中，删除 chunks 后，清理 NULL-evidence relations：

```python
# 清理无证据的幽灵关系
db.execute("""
    DELETE FROM relations
    WHERE evidence_chunk_id IS NULL
    AND (subject_id IN (
        SELECT id FROM entities WHERE space_id = %s
        AND NOT EXISTS (SELECT 1 FROM mentions WHERE entity_id = entities.id)
    ) OR object_id IN (
        SELECT id FROM entities WHERE space_id = %s
        AND NOT EXISTS (SELECT 1 FROM mentions WHERE entity_id = entities.id)
    ))
""", (document["space_id"], document["space_id"]))
```

**注意**：这个清理也可以放在 entities 清理之后，因为 entities 清理会删掉无 mention 的实体，而 relations 的 FK 引用这些实体。但因为 relations 没有 ON DELETE CASCADE，所以需要手动清理。

**更简洁的方案**：在 entities 清理时一并清理：

```python
# 删除孤立实体时，同时删除引用这些实体的关系
db.execute("""
    DELETE FROM relations
    WHERE subject_id IN (
        SELECT id FROM entities WHERE space_id = %s
        AND NOT EXISTS (SELECT 1 FROM mentions WHERE entity_id = entities.id)
    )
    OR object_id IN (
        SELECT id FROM entities WHERE space_id = %s
        AND NOT EXISTS (SELECT 1 FROM mentions WHERE entity_id = entities.id)
    )
""", (document["space_id"], document["space_id"]))
```

### 5. 文件上传顺序调整

**当前**：`upload_document()` 先保存文件到磁盘（line 138-143），再写 DB（line 147）。文件保存成功但 DB 写入失败时，文件成孤儿。

**修改**：调整为先写 DB 再存文件：

```python
# 1. 先创建 DB 记录（状态 uploading）
doc_id = str(uuid.uuid4())
store.create_document(doc_id, space_id, filename, ...)

# 2. 再保存文件
try:
    file_path = save_file(file, doc_id)
    store.update_document_path(doc_id, file_path)  # 更新文件路径
except Exception:
    store.delete_document(doc_id, owner_id, is_admin)  # 回滚 DB
    raise
```

**但**：这需要改 `create_document` 支持"预创建"状态，且 `processing.py` 依赖 `document["path"` 存在。改动较大。

**简化方案**：保持现有顺序，但在文件删除失败时添加重试日志（当前 `except OSError: pass` 改为记录警告）。文件孤儿的实际影响有限（磁盘空间），且这种情况极少发生。

## 实现顺序

1. **store.py** `delete_document()` — 添加 entities 清理 + relations 清理
2. **lightrag_bridge.py** — 新增 `delete_document()` 方法
3. **main.py** `delete_document` 端点 — 调用 LightRAG 清理
4. **store.py** `delete_document()` — 添加并发防护（拒绝 processing 状态删除）
5. **lightrag_bridge.py** `insert()` — 返回 doc_id（可选，用于后续精确删除）

## 不修的项及原因

| # | 问题 | 原因 |
|---|------|------|
| 2 | 空间删除 | 涉及前端 UI，工作量大，用户选择不修 |
| 6 | chunks 无 embeddings | 部分 embedding 失败是临时状态，worker 重试即可 |
| 7b | os.remove 失败 | 保持现状，影响极小 |
| 8 | 批量删除前端逐个调用 | 前端改动，当前够用 |
| 9 | requeue 竞态 | 低概率，保持现状 |
| 10 | 无审计日志 | 设计缺口，不在本次范围 |
| 11 | reindex 重复 | 保持现状，LightRAG 409 视为成功 |
| 12 | 遗留 SQLite | 保持现状，不影响运行 |
| 13 | relations 无 UNIQUE | 需要数据迁移，不在本次范围 |
| 14 | 实体 name 碰撞 | 设计缺口，不在本次范围 |

## 验证方式

1. 上传文档 → 确认 entities/mentions/relations/chunks 创建
2. 删除文档 → 确认 entities 表无孤儿行
3. 删除文档 → 确认 LightRAG 图谱数据被清除
4. 上传文档 → 处理中尝试删除 → 确认返回错误提示
5. 检查 relations 表 → 确认无 NULL-evidence 幽灵行
