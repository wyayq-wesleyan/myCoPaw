# 聊天文件自动解析 + DOCX 生成实现方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 聊天上传文件时自动用 MinerU 解析内容注入消息；模型可生成 DOCX 文件供用户在对话框内下载。

**Architecture:** 知识服务新增 parse API → qwenpaw MediaProcessHook 调用后注入文本 → 新增 generate_docx 工具（复用已有 send_file 发送文件，前端零改动）。

**Tech Stack:** Python FastAPI, MinerU, python-docx, React + Ant Design

## Global Constraints

- 不重建 Docker 镜像，所有容器内改动用 `docker commit`
- 知识服务容器: `huizhi-knowledge-api`，主应用容器: `huizhi-v210-check`
- python-docx 需要安装到主应用容器
- 知识服务已有 python-docx（`knowledge-service/pyproject.toml` line 12）
- 环境变量通过 `os.environ.get()` 直接在消费模块加载

---

## Task 1: 知识服务 — 新增 Parse API

**Files:**
- Modify: `knowledge-service/app/parsers.py`
- Modify: `knowledge-service/app/main.py`

**Interfaces:**
- Consumes: 现有 `parse_document()` 函数
- Produces: `extract_text_from_file(path, mineru_command) -> dict`，`POST /v1/documents/parse` 端点

- [ ] **Step 1: 在 parsers.py 新增 extract_text_from_file 函数**

在 `parsers.py` 文件末尾添加：

```python
def extract_text_from_file(path: Path, backend: str, mineru_command: str) -> dict[str, Any]:
    """Parse a file and return plain text with metadata."""
    used_backend, chunks = parse_document(path, backend, mineru_command)
    pages = set()
    parts = []
    for chunk in chunks:
        text = (chunk.get("content") or "").strip()
        if not text:
            continue
        page = chunk.get("page")
        if page is not None:
            pages.add(page)
        source_type = chunk.get("source_type", "text")
        if source_type == "table":
            parts.append(f"[表格]\n{text}")
        elif source_type == "image":
            parts.append(f"[图片] {text}")
        else:
            parts.append(text)
    return {
        "content": "\n\n".join(parts),
        "backend": used_backend,
        "pages": len(pages) if pages else None,
        "truncated": False,
    }
```

- [ ] **Step 2: 在 main.py 新增 POST /v1/documents/parse 端点**

在 `main.py` 的 `@app.delete("/v1/documents/{document_id}")` 之前添加：

```python
@app.post("/v1/documents/parse", dependencies=[Depends(require_api_key)])
async def parse_uploaded_file(file: UploadFile = File(...)) -> dict[str, Any]:
    """Parse an uploaded file and return extracted text."""
    suffix = Path(file.filename or "").suffix.lower()
    supported = {".pdf", ".doc", ".docx", ".txt", ".md", ".csv", ".xlsx", ".xls", ".json"}
    if suffix not in supported:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {suffix}")
    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (max 50MB)")
    with tempfile.TemporaryDirectory(prefix="huizhi-parse-") as tmp_dir:
        tmp_path = Path(tmp_dir) / (file.filename or f"upload{suffix}")
        tmp_path.write_bytes(data)
        try:
            result = extract_text_from_file(tmp_path, settings.parser_backend, settings.mineru_command)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Parse failed: {exc}")
    return result
```

在文件顶部确认 `tempfile` 已导入。如果没有，在 import 区添加 `import tempfile`。

- [ ] **Step 3: 验证语法**

Run: `python3 -c "import ast; ast.parse(open('knowledge-service/app/parsers.py').read()); print('parsers.py OK')" && python3 -c "import ast; ast.parse(open('knowledge-service/app/main.py').read()); print('main.py OK')"`

- [ ] **Step 4: 验证 API 可用**

在知识服务容器内测试：
```bash
docker exec huizhi-knowledge-api curl -s -X POST http://localhost:18100/v1/documents/parse \
  -H "X-Knowledge-Key: huizhi-local-knowledge-dev" \
  -F "file=@/app/working/test.txt" | python3 -m json.tool
```

---

## Task 2: qwenpaw — 修改 MediaProcessHook 调用 Parse API

**Files:**
- Modify: `src/qwenpaw/agents/utils/message_processing.py` (process_file_and_media_blocks_in_message)

**Interfaces:**
- Consumes: Task 1 的 `POST /v1/documents/parse` 端点
- Produces: 文件上传时自动注入解析文本

- [ ] **Step 1: 添加 parse_file_via_knowledge_service 辅助函数**

在 `message_processing.py` 文件顶部（import 区之后）添加：

```python
import os
import httpx

_KNOWLEDGE_PARSE_URL = ""
_KNOWLEDGE_PARSE_KEY = ""

def _get_knowledge_parse_config():
    global _KNOWLEDGE_PARSE_URL, _KNOWLEDGE_PARSE_KEY
    if not _KNOWLEDGE_PARSE_URL:
        _KNOWLEDGE_PARSE_URL = os.environ.get("QWENPAW_KNOWLEDGE_URL", "")
        _KNOWLEDGE_PARSE_KEY = os.environ.get("QWENPAW_KNOWLEDGE_API_KEY", "huizhi-local-knowledge-dev")
    return _KNOWLEDGE_PARSE_URL, _KNOWLEDGE_PARSE_KEY


async def _parse_file_via_knowledge(file_path: str, filename: str) -> str | None:
    """Call knowledge service parse API. Returns parsed text or None on failure."""
    url, key = _get_knowledge_parse_config()
    if not url:
        return None
    parse_url = f"{url.rstrip('/')}/v1/documents/parse"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            with open(file_path, "rb") as f:
                response = await client.post(
                    parse_url,
                    headers={"X-Knowledge-Key": key},
                    files={"file": (filename, f)},
                )
            if response.status_code != 200:
                return None
            data = response.json()
            return data.get("content")
    except Exception:
        return None
```

- [ ] **Step 2: 修改文件处理逻辑，调用 parse API**

在 `message_processing.py` 中找到 `process_file_and_media_blocks_in_message` 函数。在函数末尾、`freeze_local_images_async` 调用之前，找到插入文本块的代码段（大约在 lines 536-548）：

```python
# 现有代码：
message.content.insert(i + 1, TextBlock(type="text", text=text))
```

将其替换为：

```python
# 新代码：对文档类文件调用 parse API
non_media_exts = {".pdf", ".doc", ".docx", ".txt", ".md", ".csv", ".xlsx", ".xls", ".json"}
suffix = Path(local_path).suffix.lower()
if suffix in non_media_exts:
    parsed_text = await _parse_file_via_knowledge(str(local_path), Path(local_path).name)
    if parsed_text:
        text = (
            f"用户上传了文件：{Path(local_path).name}\n\n"
            f"--- 文件内容开始 ---\n"
            f"{parsed_text}\n"
            f"--- 文件内容结束 ---"
        )
    else:
        text = f"用户上传文件，已经下载到 {local_path}"
else:
    text = f"用户上传文件，已经下载到 {local_path}"
message.content.insert(i + 1, TextBlock(type="text", text=text))
```

- [ ] **Step 3: 验证语法**

Run: `python3 -c "import ast; ast.parse(open('src/qwenpaw/agents/utils/message_processing.py').read()); print('OK')"`

---

## Task 3: qwenpaw — 新增 generate_docx 工具

**Files:**
- Create: `src/qwenpaw/agents/tools/generate_docx.py`
- Modify: `src/qwenpaw/agents/tools/__init__.py` (注册工具)

**Interfaces:**
- Consumes: python-docx 库
- Produces: `generate_docx(content, filename) -> str`（返回 `file://` 绝对路径），模型随后调用已有 `send_file` 工具发送给用户

> **设计说明**：`generate_docx` 只负责"生成文件"，返回 `file://` 绝对路径。文件发送复用已有 `send_file` 工具（`src/qwenpaw/agents/tools/send_file.py`），它已处理 `DataBlock(source=URLSource(...))` 封装、MIME 检测、前端 `<Files>` 下载卡片渲染（`HostBubbles.tsx:108-121` + `toDisplayUrl()`）。前端**零改动**。工具描述中明确引导模型"生成后用 send_file 发送"。

- [ ] **Step 1: 创建 generate_docx.py**

```python
"""Tool to generate DOCX files from Markdown content."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from ...runtime.tool_registry import tool_descriptor


def _resolve_media_dir() -> Path:
    """Resolve the same media directory used by chat uploads.

    Mirrors ``ConsoleChannel._media_dir`` resolution (channel.py:112-117):
    ``{workspace_dir}/media`` when workspace_dir is set, else DEFAULT_MEDIA_DIR.
    """
    from ...app.channels.console.channel import DEFAULT_MEDIA_DIR

    workspace_dir = os.environ.get("QWENPAW_WORKSPACE_DIR", "").strip()
    media_dir = (
        Path(workspace_dir) / "media"
        if workspace_dir
        else DEFAULT_MEDIA_DIR
    )
    media_dir.mkdir(parents=True, exist_ok=True)
    return media_dir


def _markdown_to_docx(content: str, output_path: Path) -> None:
    """Convert markdown text to a DOCX file."""
    from docx import Document
    from docx.shared import Pt

    doc = Document()

    # Set default font
    style = doc.styles["Normal"]
    font = style.font
    font.name = "Microsoft YaHei"
    font.size = Pt(11)

    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()

        # Heading
        if line.startswith("# "):
            doc.add_heading(line[2:], level=1)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=2)
        elif line.startswith("### "):
            doc.add_heading(line[4:], level=3)
        elif line.startswith("#### "):
            doc.add_heading(line[5:], level=4)
        # Unordered list
        elif re.match(r"^[-*]\s+", line):
            text = re.sub(r"^[-*]\s+", "", line)
            doc.add_paragraph(text, style="List Bullet")
        # Ordered list
        elif re.match(r"^\d+\.\s+", line):
            text = re.sub(r"^\d+\.\s+", "", line)
            doc.add_paragraph(text, style="List Number")
        # Table (detect | delimiter)
        elif line.startswith("|") and "|" in line[1:]:
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            i -= 1  # Will be incremented at end of loop
            if len(table_lines) >= 2:
                rows = []
                for tl in table_lines:
                    cells = [c.strip() for c in tl.strip("|").split("|")]
                    # Skip separator row (---|---|---)
                    if all(re.match(r"^[-:]+$", c) for c in cells):
                        continue
                    rows.append(cells)
                if rows:
                    num_cols = max(len(r) for r in rows)
                    table = doc.add_table(rows=len(rows), cols=num_cols)
                    table.style = "Table Grid"
                    for ri, row_data in enumerate(rows):
                        for ci, cell_text in enumerate(row_data):
                            if ci < num_cols:
                                table.rows[ri].cells[ci].text = cell_text
        # Horizontal rule
        elif re.match(r"^[-*_]{3,}\s*$", line):
            doc.add_paragraph("─" * 50)
        # Empty line
        elif not line.strip():
            pass
        # Normal paragraph
        else:
            doc.add_paragraph(line)

        i += 1

    doc.save(str(output_path))


def _path_to_file_url(path: Path) -> str:
    """Convert a local file path to a ``file://`` URL (same as file_io.py:34)."""
    abs_path = str(path.resolve())
    if os.name == "nt":
        abs_path = abs_path.replace("\\", "/")
        return f"file:///{abs_path}"
    return f"file://{abs_path}"


@tool_descriptor(
    name="generate_docx",
    async_execution=False,
    tool_type="file",
    policy_name="GenerateDocx",
    default_policy="allow",
    policy_reason="Generates a DOCX file for user download",
    ui_description="将 Markdown 内容转换为 DOCX 文件，随后必须调用 send_file 发送给用户",
    ui_icon="📄",
)
def generate_docx(content: str, filename: str = "document") -> ToolChunk:
    """Generate a DOCX file from markdown content. Returns the file path —
    call ``send_file`` with the returned path to deliver the file to the user.

    Args:
        content: Markdown format document content
        filename: Output filename without extension
    """
    if not content or not content.strip():
        return ToolChunk(
            is_last=True,
            state=ToolResultState.ERROR,
            content=[TextBlock(type="text", text="内容不能为空")],
        )

    # Sanitize filename
    safe_name = re.sub(r'[<>:"/\\|?*]', "_", filename)[:100] or "document"
    stored_name = f"{uuid.uuid4().hex}_{safe_name}.docx"

    media_dir = _resolve_media_dir()
    output_path = media_dir / stored_name

    try:
        _markdown_to_docx(content, output_path)
    except ImportError:
        return ToolChunk(
            is_last=True,
            state=ToolResultState.ERROR,
            content=[TextBlock(type="text", text="python-docx 未安装，无法生成 DOCX 文件")],
        )
    except Exception as exc:
        return ToolChunk(
            is_last=True,
            state=ToolResultState.ERROR,
            content=[TextBlock(type="text", text=f"生成 DOCX 失败: {exc}")],
        )

    file_url = _path_to_file_url(output_path)
    return ToolChunk(
        is_last=True,
        state=ToolResultState.SUCCESS,
        content=[TextBlock(
            type="text",
            text=f"DOCX 文件已生成：{safe_name}.docx\n路径：{file_url}\n请调用 send_file 工具，参数 file_path 填上述路径，将文件发送给用户。",
        )],
    )
```

- [ ] **Step 2: 注册工具到 __init__.py**

在 `src/qwenpaw/agents/tools/__init__.py` 中，找到现有工具导入列表（`from . import ...`），添加：

```python
from . import generate_docx
```

- [ ] **Step 3: 验证语法**

Run: `python3 -c "import ast; ast.parse(open('src/qwenpaw/agents/tools/generate_docx.py').read()); print('generate_docx.py OK')" && python3 -c "import ast; ast.parse(open('src/qwenpaw/agents/tools/__init__.py').read()); print('__init__.py OK')"`

---

## Task 4: 主应用容器 — 安装 python-docx

**Files:**
- Modify: 主应用容器内安装 python-docx

**Interfaces:**
- Consumes: pip
- Produces: python-docx 可用

- [ ] **Step 1: 在主应用容器内安装 python-docx**

```bash
docker exec huizhi-v210-check pip install python-docx>=1.1
```

- [ ] **Step 2: 验证安装**

```bash
docker exec huizhi-v210-check python3 -c "import docx; print('python-docx OK')"
```

---

## 涉及文件汇总

| 文件 | 改动 |
|------|------|
| `knowledge-service/app/parsers.py` | 新增 `extract_text_from_file()` |
| `knowledge-service/app/main.py` | 新增 `POST /v1/documents/parse` 端点 |
| `src/qwenpaw/agents/utils/message_processing.py` | 修改文件处理逻辑：调用 parse API + 注入文本 |
| `src/qwenpaw/agents/tools/generate_docx.py` | 新建：generate_docx 工具（返回 `file://` 路径） |
| `src/qwenpaw/agents/tools/__init__.py` | 注册新工具 |
| 主应用容器 | 安装 python-docx |

> **前端零改动**：文件下载复用已有 `send_file` 工具 + 前端 `<Files>` 下载卡片（`HostBubbles.tsx:108-121`、`toDisplayUrl()`）。

## 验证方式

1. 上传 PDF 到聊天 → 确认消息中注入了解析文本（含"--- 文件内容开始 ---"标记）
2. 上传 DOCX 到聊天 → 确认解析内容正确
3. 让模型修改文件内容 → 确认调用 generate_docx → 再调用 send_file → 对话框显示下载卡片
4. 点击下载 → 确认 DOCX 文件可下载且内容正确
5. 知识服务不可用时上传文件 → 确认降级为仅传路径（"用户上传文件，已经下载到 {path}"）
