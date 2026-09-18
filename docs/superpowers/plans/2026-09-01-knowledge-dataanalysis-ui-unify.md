# 知识空间 & 数据分析 UI 统一改进方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为知识空间和数据分析的文档/任务列表添加处理时间统计、状态列、状态过滤、列排序，统一待处理列表体验。

**Architecture:** 后端新增 `started_at` 字段记录处理开始时间；前端统一两个模块的列表体验：状态列、状态过滤、列排序、待处理优先排序。

**Tech Stack:** PostgreSQL, Python FastAPI, React + Ant Design Table, TypeScript

---

## 全局约束

- 不重建 Docker 镜像，所有容器内改动用 `docker commit`
- 前端改动在 `console/src/` 目录
- 后端改动在 `knowledge-service/app/` 和 `src/qwenpaw/data_tables/`
- 使用 Ant Design `<Table>` 的 `sorter` / `filters` / `filteredValue` 属性
- 保持现有代码风格，不引入新依赖

---

## 任务拆分

### Task 1: Knowledge 后端 — 添加 started_at 字段

**文件:**
- Modify: `knowledge-service/app/store.py` (documents 表 schema + claim_job + get_document_stats)

**接口:**
- Consumes: 现有 `documents` 表
- Produces: `documents.started_at` 字段, stats 返回 `started_at` / `completed_at` / `processing_duration`

- [ ] **Step 1: 修改 documents 表 schema，添加 started_at 字段**

在 `store.py` 的 `CREATE TABLE IF NOT EXISTS documents` 中添加：
```sql
started_at TIMESTAMPTZ
```

- [ ] **Step 2: 修改 claim_job 方法，设置 started_at**

在 `store.py` 的 `claim_job()` 方法中，UPDATE documents 时设置 `started_at`：
```python
db.execute(
    "UPDATE documents SET status = 'processing', started_at = COALESCE(started_at, %s), updated_at = %s WHERE id = %s",
    (timestamp, timestamp, row["document_id"]),
)
```

- [ ] **Step 3: 修改 get_document_stats，返回时间字段**

在 `store.py` 的 `get_document_stats()` 方法中，返回值添加：
```python
return {
    ...existing fields...,
    "created_at": document["created_at"].isoformat() if document.get("created_at") else None,
    "started_at": document["started_at"].isoformat() if document.get("started_at") else None,
    "completed_at": document["updated_at"].isoformat() if document.get("status") == "completed" else None,
    "processing_duration": (
        (document["updated_at"] - document["started_at"]).total_seconds()
        if document.get("started_at") and document.get("status") in ("completed", "failed")
        else None
    ),
}
```

- [ ] **Step 4: 修改 list_documents，返回 started_at**

确认 `list_documents()` 返回的行已包含 `started_at`（因为是 SELECT *）。

- [ ] **Step 5: 验证语法正确**

Run: `python3 -c "import ast; ast.parse(open('knowledge-service/app/store.py').read()); print('OK')"`

---

### Task 2: Knowledge 前端 — 更新类型定义 + 统计面板

**文件:**
- Modify: `console/src/api/modules/knowledge.ts` (类型定义)
- Modify: `console/src/pages/Knowledge/index.tsx` (StatsDrawer)

**接口:**
- Consumes: Task 1 产出的 stats API 新字段
- Produces: `KnowledgeDocumentStats` 新增时间字段, StatsDrawer 显示处理时间

- [ ] **Step 1: 更新 KnowledgeDocumentStats 类型**

在 `knowledge.ts` 中更新接口：
```typescript
export interface KnowledgeDocumentStats {
  document_id: string;
  filename: string;
  status: string;
  size: number;
  chunk_count: number;
  entity_count: number;
  relation_count: number;
  created_at?: string;
  started_at?: string;
  completed_at?: string;
  processing_duration?: number;  // seconds
}
```

- [ ] **Step 2: 更新 StatsDrawer 显示时间信息**

在 `Knowledge/index.tsx` 的 StatsDrawer 组件中，添加时间统计行：
```tsx
<Row gutter={[16, 16]}>
  {/* 现有字段 */}
  <Col span={12}><Statistic title="文件大小" value={formatFileSize(stats.size)} /></Col>
  <Col span={12}><Statistic title="状态" value={statusMeta[stats.status]?.label || stats.status} /></Col>
  <Col span={12}><Statistic title="分块数" value={stats.chunk_count} /></Col>
  <Col span={12}><Statistic title="实体数" value={stats.entity_count} /></Col>
  <Col span={12}><Statistic title="关系数" value={stats.relation_count} /></Col>
  {/* 新增时间字段 */}
  <Col span={12}><Statistic title="上传时间" value={stats.created_at ? dayjs(stats.created_at).format('YYYY-MM-DD HH:mm:ss') : '-'} /></Col>
  <Col span={12}><Statistic title="开始处理" value={stats.started_at ? dayjs(stats.started_at).format('YYYY-MM-DD HH:mm:ss') : '-'} /></Col>
  <Col span={12}><Statistic title="处理完成" value={stats.completed_at ? dayjs(stats.completed_at).format('YYYY-MM-DD HH:mm:ss') : '-'} /></Col>
  <Col span={12}><Statistic title="处理时长" value={stats.processing_duration != null ? `${stats.processing_duration.toFixed(1)}s` : '-'} /></Col>
</Row>
```

- [ ] **Step 3: 确保 dayjs 已导入**

检查 `Knowledge/index.tsx` 是否已导入 `dayjs`，如未导入则添加。

- [ ] **Step 4: 验证语法正确**

Run: `cd console && npx tsc --noEmit 2>&1 | head -20`

---

### Task 3: DataAnalysis 后端 — 添加 started_at 字段

**文件:**
- Modify: `src/qwenpaw/data_tables/store.py` (data_import_job 表 schema)
- Modify: `src/qwenpaw/data_tables/importer.py` (设置 started_at)

**接口:**
- Consumes: 现有 `data_import_job` 表
- Produces: `data_import_job.started_at` 字段, list API 返回 `started_at` + 计算 duration

- [ ] **Step 1: 修改 data_import_job 表 schema，添加 started_at 字段**

在 `store.py` 的 `CREATE TABLE IF NOT EXISTS data_import_job` 中添加：
```sql
started_at TIMESTAMPTZ
```

- [ ] **Step 2: 在 importer.py 中设置 started_at**

在 `importer.py` 的导入处理函数中，开始处理时设置 `started_at`：
```python
db.execute(
    "UPDATE data_import_job SET status = 'parsing', started_at = COALESCE(started_at, now()) WHERE id = %s",
    (job_id,),
)
```

- [ ] **Step 3: 修改 list import jobs API，返回 started_at 和 duration**

在 `import_api.py` 的 list jobs 查询中，SELECT 添加 `started_at`，返回时计算：
```python
"started_at": row["started_at"].isoformat() if row.get("started_at") else None,
"processing_duration": (
    (row["finished_at"] - row["started_at"]).total_seconds()
    if row.get("started_at") and row.get("finished_at") else None
),
```

- [ ] **Step 4: 验证语法正确**

Run: `python3 -c "import ast; ast.parse(open('src/qwenpaw/data_tables/store.py').read()); print('OK')"`

---

### Task 4: DataAnalysis 前端 — 更新类型 + 主表状态列 + 状态过滤

**文件:**
- Modify: `console/src/api/modules/dataAnalysis.ts` (类型定义)
- Modify: `console/src/pages/DataAnalysis/index.tsx` (主表 + 筛选)

**接口:**
- Consumes: Task 3 产出的 API 新字段
- Produces: 主表显示状态列, 状态过滤器, 移除 badge, 列排序

- [ ] **Step 1: 更新 DataImportJob 类型**

在 `dataAnalysis.ts` 中更新接口：
```typescript
export interface DataImportJob {
  id: string;
  table_id: string;
  table_name: string;
  filename: string;
  status: string;
  total_rows: number;
  success_rows: number;
  failed_rows: number;
  errors: unknown[];
  created_at: string;
  finished_at: string | null;
  started_at?: string | null;
  processing_duration?: number | null;
}
```

- [ ] **Step 2: 主表添加状态列**

在 `DataAnalysis/index.tsx` 的 `columns` 定义中，在"名称"列之后添加状态列：
```typescript
{
  title: "状态",
  dataIndex: "initialized",
  key: "status",
  width: 100,
  render: (_: unknown, record: DataTableItem) => (
    <Tag color={record.initialized ? "success" : "default"}>
      {record.initialized ? "已导入" : "待导入"}
    </Tag>
  ),
  filters: [
    { text: "已导入", value: true },
    { text: "待导入", value: false },
  ],
  onFilter: (value: unknown, record: DataTableItem) => record.initialized === value,
},
```

- [ ] **Step 3: 移除 Jobs 按钮的 badge**

在 `DataAnalysis/index.tsx` 中，找到 Jobs 按钮上的 `<Badge count={...}>`，移除 Badge 包裹，仅保留 Button。

- [ ] **Step 4: 所有列添加 sorter**

为每个列添加 `sorter` 属性：
```typescript
// 名称列
sorter: (a: DataTableItem, b: DataTableItem) => a.display_name.localeCompare(b.display_name),

// 描述列
sorter: (a: DataTableItem, b: DataTableItem) => (a.description || '').localeCompare(b.description || ''),

// 列数列
sorter: (a: DataTableItem, b: DataTableItem) => a.column_count - b.column_count,

// 行数列
sorter: (a: DataTableItem, b: DataTableItem) => a.row_count - b.row_count,

// 创建时间列
sorter: (a: DataTableItem, b: DataTableItem) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
```

- [ ] **Step 5: 排序默认将待处理/处理中置顶**

添加默认排序：
```typescript
// 在 Table 组件上添加
defaultSortOrder="ascend"
sortSortDirections={['ascend', 'descend']}
```

或者使用自定义 sortOrder 在状态列上：
```typescript
{
  title: "状态",
  ...existing,
  defaultSortOrder: "ascend",
  sortOrder: undefined, // 受控模式需用 state
  sorter: (a: DataTableItem, b: DataTableItem) => {
    const order = { false: 0, true: 1 };  // 待导入优先
    return (order[a.initialized ? 'true' : 'false'] || 0) - (order[b.initialized ? 'true' : 'false'] || 0);
  },
}
```

- [ ] **Step 6: 验证语法正确**

Run: `cd console && npx tsc --noEmit 2>&1 | head -20`

---

### Task 5: Knowledge 前端 — 列排序 + 待处理优先

**文件:**
- Modify: `console/src/pages/Knowledge/index.tsx` (表格列定义)

**接口:**
- Consumes: 现有 KnowledgeDocument 类型
- Produces: 所有列可排序, 待处理/处理中置顶

- [ ] **Step 1: 所有列添加 sorter**

```typescript
// 序号列 (不需要 sorter)

// 文件名列
sorter: (a: KnowledgeDocument, b: KnowledgeDocument) => a.filename.localeCompare(b.filename),

// 大小列
sorter: (a: KnowledgeDocument, b: KnowledgeDocument) => (a.size || 0) - (b.size || 0),

// 状态列 — 同时作为默认排序列，待处理优先
sorter: (a: KnowledgeDocument, b: KnowledgeDocument) => {
  const order = { queued: 0, processing: 1, failed: 2, completed: 3 };
  return (order[a.status] ?? 4) - (order[b.status] ?? 4);
},
defaultSortOrder: "ascend",

// 创建时间列
sorter: (a: KnowledgeDocument, b: KnowledgeDocument) => 
  new Date(a.created_at || 0).getTime() - new Date(b.created_at || 0).getTime(),
```

- [ ] **Step 2: 验证语法正确**

Run: `cd console && npx tsc --noEmit 2>&1 | head -20`

---

### Task 6: DataAnalysis Jobs Drawer — 添加时间列 + 排序

**文件:**
- Modify: `console/src/pages/DataAnalysis/index.tsx` (jobs drawer 表格列)

**接口:**
- Consumes: Task 4 更新的 DataImportJob 类型
- Produces: jobs 表格显示处理时间, 所有列可排序

- [ ] **Step 1: Jobs 表格添加处理时长列**

在 `jobColumns` 定义中添加：
```typescript
{
  title: "处理时长",
  key: "processing_duration",
  width: 100,
  render: (_: unknown, record: DataImportJob) => {
    if (record.processing_duration != null) {
      return `${record.processing_duration.toFixed(1)}s`;
    }
    if (record.started_at && !record.finished_at) {
      return <span style={{ color: '#1890ff' }}>处理中...</span>;
    }
    return '-';
  },
  sorter: (a: DataImportJob, b: DataImportJob) => 
    (a.processing_duration || 0) - (b.processing_duration || 0),
},
```

- [ ] **Step 2: Jobs 表格所有列添加 sorter**

为现有列（表名、文件名、状态、总行数、成功行数、失败行数、创建时间）添加 sorter。

- [ ] **Step 3: 验证语法正确**

Run: `cd console && npx tsc --noEmit 2>&1 | head -20`

---

## 涉及文件汇总

| 文件 | 改动 |
|------|------|
| `knowledge-service/app/store.py` | 添加 `started_at` 字段, claim_job 设置, stats 返回时间 |
| `console/src/api/modules/knowledge.ts` | KnowledgeDocumentStats 添加时间字段 |
| `console/src/pages/Knowledge/index.tsx` | StatsDrawer 时间显示, 列排序 |
| `src/qwenpaw/data_tables/store.py` | data_import_job 添加 `started_at` |
| `src/qwenpaw/data_tables/importer.py` | 设置 started_at |
| `src/qwenpaw/data_tables/import_api.py` | list jobs 返回 started_at + duration |
| `console/src/api/modules/dataAnalysis.ts` | DataImportJob 添加时间字段 |
| `console/src/pages/DataAnalysis/index.tsx` | 主表状态列, 状态过滤, 移除 badge, 列排序, jobs 时间列 |

## 验证方式

1. 上传文档到知识空间 → 查看统计面板 → 确认显示上传时间、开始处理时间、处理完成时间、处理时长
2. 上传数据到数据分析 → 查看列表 → 确认有状态列、可过滤、可排序
3. 点击任意列头 → 确认排序生效
4. 确认待处理/处理中条目排在列表前面
5. 确认数据分析 Jobs 按钮无 badge 角标
6. 确认两个模块的 Jobs/任务列表都有处理时长列
