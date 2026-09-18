# 聊天文件自动解析 + DOCX 生成设计

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 聊天上传文件时自动用 MinerU 解析内容注入消息；模型可生成 DOCX 文件供用户在对话框内下载。

**Architecture:** 知识服务新增 parse API → qwenpaw MediaProcessHook 调用后注入文本 → 新增 generate_docx 工具 → 前端渲染下载链接。

**Tech Stack:** Python FastAPI, MinerU, python-docx, React

---

## 一、知识服务：新增 Parse API

### 端点

`POST /v1/documents/parse` — 需要 API Key 认证

### 请求

`multipart/form-data`:
- `file`: 文件内容（必填）

### 响应

```json
{
  "content": "提取的纯文本...",
  "backend": "mineru",
  "pages": 12,
  "truncated": false
}
```

### 错误码

- `400`: 不支持的文件格式
- `413`: 文件过大
- `503`: MinerU 不可用

### 实现

在 `knowledge-service/app/parsers.py` 中，已有 `parse_document()` 返回 `list[dict]`（每个 chunk 有 page/content）。新增 `extract_text_from_file(path, mineru_command) -> str` 函数，将 chunks 拼接为纯文本。

在 `knowledge-service/app/main.py` 中新增端点，复用现有 `parse_document()` 逻辑。

---

## 二、qwenpaw：修改 MediaProcessHook

### 当前行为

`src/qwenpaw/hooks/request_setup/media_hook.py` 中 `process_file_and_media_blocks_in_message()`:
- 图片/视频/音频 → 直接传给模型
- 其他文件 → 插入文本 `"用户上传文件，已经下载到 {path}"`

### 新行为

```
文件是图片/视频/音频 → 保持现有逻辑
文件是文档（PDF/DOCX/TXT/MD/CSV/XLSX）→ 调用知识服务 parse API
  → 成功：注入解析文本
  → 失败：降级为现有行为（只传路径）
```

### 注入格式

```
用户上传了文件：report.pdf（12页）

--- 文件内容开始 ---
[MinerU 解析的纯文本内容，保留段落结构]
--- 文件内容结束 ---
```

### 配置

新增环境变量：
- `KNOWLEDGE_SERVICE_URL`: 知识服务地址（如 `http://huizhi-knowledge-api:18100`）
- `KNOWLEDGE_SERVICE_KEY`: API Key（默认 `huizhi-local-knowledge-dev`）

---

## 三、qwenpaw：新增 generate_docx 工具

### 工具定义

```python
{
    "name": "generate_docx",
    "description": "将 Markdown 内容转换为 DOCX 文件并返回下载链接。用于用户要求修改/生成文档时。",
    "parameters": {
        "content": "Markdown 格式的文档内容",
        "filename": "输出文件名（不含扩展名）"
    }
}
```

### 实现

在 `src/qwenpaw/agents/tools/` 中新增 `generate_docx.py`：

1. 接收 Markdown 文本
2. 用 `python-docx` 转换为 DOCX：
   - `# 标题` → Heading 1
   - `## 标题` → Heading 2
   - 普通段落 → Paragraph
   - `- 列表项` → List Bullet
   - `| 表格 |` → Table
3. 保存到 workspace media 目录：`{media_dir}/{uuid}_{filename}.docx`
4. 返回下载 URL：`/files/preview/{stored_name}`

### 依赖

在 qwenpaw 的 `requirements.txt` 或 `pyproject.toml` 中添加 `python-docx`。

---

## 四、前端：渲染下载链接

当模型调用 `generate_docx` 返回 URL 时，assistant 消息中会包含工具调用结果。前端需要识别文件下载类工具的返回值，渲染为可点击的下载链接。

### 渲染逻辑

在 `console/src/pages/Chat/index.tsx` 中，检测 assistant 消息中的工具调用结果：
- 如果工具名是 `generate_docx` 且返回了 URL
- 渲染为 `<a href={url} download>📄 {filename}.docx</a>` 样式的下载卡片

---

## 五、文件改动汇总

| 文件 | 改动 |
|------|------|
| `knowledge-service/app/parsers.py` | 新增 `extract_text_from_file()` |
| `knowledge-service/app/main.py` | 新增 `POST /v1/documents/parse` 端点 |
| `src/qwenpaw/hooks/request_setup/media_hook.py` | 修改文件处理：调用 parse API + 注入文本 |
| `src/qwenpaw/agents/tools/generate_docx.py` | 新增 generate_docx 工具 |
| `src/qwenpaw/agents/tools/__init__.py` | 注册新工具 |
| `console/src/pages/Chat/index.tsx` | 渲染文件下载链接 |

## 六、验证方式

1. 上传 PDF 到聊天 → 确认消息中注入了解析文本（而非仅路径）
2. 上传 DOCX 到聊天 → 确认解析内容正确
3. 让模型修改文件 → 确认调用 generate_docx → 对话框显示下载链接
4. 下载生成的 DOCX → 确认内容正确、格式基本保留
5. MinerU 不可用时上传文件 → 确认降级为仅传路径
