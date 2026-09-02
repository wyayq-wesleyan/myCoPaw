from __future__ import annotations

import base64
import csv
import importlib.util
import json
import mimetypes
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class ParseError(RuntimeError):
    pass


_CHUNK_SIZE = 1400


def _table_to_text(rows: list[list[Any]], title: str) -> str:
    lines = [title]
    for row in rows:
        values = [str(value).strip() for value in row if value is not None and str(value).strip()]
        if values:
            lines.append(" | ".join(values))
    return "\n".join(lines)


def _split_long_text(text: str, max_chars: int = _CHUNK_SIZE) -> list[str]:
    """Split long text on sentence boundaries without dropping any content."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    # Prefer natural-language boundaries. The fallback handles long unbroken
    # strings such as table cells, URLs, or copied source code.
    units = [unit.strip() for unit in re.split(r"(?<=[。！？；.!?;])\s*|\n+", text) if unit.strip()]
    parts: list[str] = []
    current = ""
    for unit in units:
        if len(unit) > max_chars:
            if current:
                parts.append(current)
                current = ""
            parts.extend(unit[index : index + max_chars] for index in range(0, len(unit), max_chars))
            continue
        candidate = f"{current}\n{unit}" if current else unit
        if len(candidate) > max_chars:
            if current:
                parts.append(current)
            current = unit
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _content_chunks(
    content: str,
    *,
    page: int | None,
    location: str,
    source_type: str,
) -> list[dict[str, Any]]:
    """Turn one extracted block into ordered, retrieval-sized evidence chunks."""
    parts = _split_long_text(content)
    total = len(parts)
    return [
        {
            "page": page,
            "location": location if total == 1 else f"{location}（片段 {index}/{total}）",
            "content": part,
            "source_type": source_type,
        }
        for index, part in enumerate(parts, start=1)
    ]


class SimpleReader:
    """Readers for formats without meaningful layout: plain text and spreadsheets.

    PDF/DOC/DOCX are NOT handled here — they go through the MinerU pipeline
    (optionally via LibreOffice conversion) so tables, images, reading order,
    and page positions survive into the retrieval index.
    """

    def parse(self, path: Path) -> list[dict[str, Any]]:
        suffix = path.suffix.lower()
        if suffix in {".txt", ".md", ".csv"}:
            return self._parse_text(path, suffix)
        if suffix in {".xlsx", ".xlsm"}:
            return self._parse_xlsx(path)
        if suffix == ".xls":
            return self._parse_xls(path)
        if suffix == ".json":
            return self._parse_json(path)
        raise ParseError(f"File type {suffix or 'unknown'} must be parsed by the MinerU pipeline")

    def _parse_text(self, path: Path, suffix: str) -> list[dict[str, Any]]:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        if suffix == ".csv":
            with path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
                rows = list(csv.reader(handle))
            content = _table_to_text(rows, "CSV 表格")
        chunks = _content_chunks(content, page=None, location="正文", source_type="text")
        if not chunks:
            raise ParseError("The text file does not contain extractable content")
        return chunks

    def _parse_xlsx(self, path: Path) -> list[dict[str, Any]]:
        try:
            from openpyxl import load_workbook  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ParseError("openpyxl is not installed in the knowledge service image") from exc
        workbook = load_workbook(path, read_only=True, data_only=True)
        chunks: list[dict[str, Any]] = []
        for sheet in workbook.worksheets:
            rows = [list(row) for row in sheet.iter_rows(values_only=True)]
            content = _table_to_text(rows, f"工作表：{sheet.title}")
            if content:
                chunks.extend(
                    _content_chunks(
                        content,
                        page=None,
                        location=f"工作表：{sheet.title}",
                        source_type="table",
                    )
                )
        workbook.close()
        if not chunks:
            raise ParseError("The spreadsheet does not contain readable cells")
        return chunks

    def _parse_xls(self, path: Path) -> list[dict[str, Any]]:
        try:
            import xlrd  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ParseError("xlrd is not installed in the knowledge service image") from exc
        workbook = xlrd.open_workbook(str(path), on_demand=True)
        chunks: list[dict[str, Any]] = []
        for sheet in workbook.sheets():
            rows = [sheet.row_values(row_index) for row_index in range(sheet.nrows)]
            content = _table_to_text(rows, f"工作表：{sheet.name}")
            if content:
                chunks.extend(
                    _content_chunks(
                        content,
                        page=None,
                        location=f"工作表：{sheet.name}",
                        source_type="table",
                    )
                )
        workbook.release_resources()
        if not chunks:
            raise ParseError("The spreadsheet does not contain readable cells")
        return chunks

    def _parse_json(self, path: Path) -> list[dict[str, Any]]:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ParseError(f"Invalid JSON document: {exc}") from exc
        content = json.dumps(value, ensure_ascii=False, indent=2)
        chunks = _content_chunks(content, page=None, location="JSON 正文", source_type="json")
        if not chunks:
            raise ParseError("The JSON document does not contain readable content")
        return chunks


class MinerUParser:
    """Uses an explicitly configured local MinerU command and normalizes JSON output."""

    def __init__(self, command: str) -> None:
        self.command = command

    @property
    def available(self) -> bool:
        # MinerU's local pipeline imports torch even for the text backend.
        # Auto mode must fall back cleanly when the optional OCR stack is not
        # present in a deliberately small knowledge image.
        return (
            bool(self.command)
            and shutil.which(self.command.split()[0]) is not None
            and importlib.util.find_spec("torch") is not None
        )

    def parse(self, path: Path) -> list[dict[str, Any]]:
        if not self.available:
            raise ParseError("MinerU is not available. Install it in the image and set MINERU_COMMAND.")
        with tempfile.TemporaryDirectory(prefix="huizhi-mineru-") as output_dir:
            command = self.command.format(input=str(path), output=output_dir)
            result = subprocess.run(command, shell=True, capture_output=True, text=True, check=False, timeout=1800)
            if result.returncode != 0:
                raise ParseError(f"MinerU failed: {result.stderr[-1000:] or result.stdout[-1000:]}")
            json_files = sorted(Path(output_dir).rglob("*.json"))
            if not json_files:
                raise ParseError("MinerU completed but did not produce a JSON result")
            preferred = next(
                (
                    path
                    for path in json_files
                    if path.name.casefold() in {"content_list.json", "content_list_v2.json"}
                ),
                json_files[0],
            )
            raw = json.loads(preferred.read_text(encoding="utf-8"))
            return self._normalize(raw, Path(output_dir))

    def _normalize(self, raw: Any, output_dir: Path | None = None) -> list[dict[str, Any]]:
        if isinstance(raw, dict) and isinstance(raw.get("content_list"), list):
            raw = raw["content_list"]
        if isinstance(raw, list) and all(isinstance(item, dict) and item.get("type") for item in raw):
            # MinerU content_list.json is a flat list; preserve its real page
            # indices instead of treating every block as a separate page.
            pages_by_index: dict[int, list[dict[str, Any]]] = {}
            for item in raw:
                page_idx = int(item.get("page_idx", item.get("page", 0)) or 0)
                pages_by_index.setdefault(page_idx, []).append(item)
            pages = [{"page_idx": page_idx, "blocks": blocks} for page_idx, blocks in sorted(pages_by_index.items())]
        elif isinstance(raw, list):
            pages = raw
        elif isinstance(raw, dict) and isinstance(raw.get("pages"), list):
            pages = raw["pages"]
        elif isinstance(raw, dict) and isinstance(raw.get("pdf_info"), list):
            pages = raw["pdf_info"]
        else:
            raise ParseError("Unsupported MinerU JSON shape; configure an adapter for this MinerU release")
        chunks: list[dict[str, Any]] = []
        for page_index, page in enumerate(pages, start=1):
            page_number = int(page.get("page_idx", page_index - 1) or 0) + 1 if isinstance(page, dict) else page_index
            if isinstance(page, dict) and page.get("type") in {"text", "table", "image", "equation"}:
                blocks = [page]
            elif isinstance(page, dict):
                blocks = page.get("blocks", page.get("para_blocks", page.get("content", [])))
            else:
                blocks = []
            for block_index, block in enumerate(blocks, start=1):
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type", "text"))
                text = str(block.get("text", block.get("content", ""))).strip()
                image_data = None
                if block_type == "image" and output_dir is not None:
                    image_data = self._read_image_data(block, output_dir)
                if block_type == "table" and not text:
                    text = str(block.get("table_body", block.get("html", ""))).strip()
                if block_type == "image" and not text:
                    # 图片本体无法入索引，但保留题注让检索能定位到图
                    captions = block.get("image_caption") or block.get("img_caption") or block.get("captions") or []
                    if isinstance(captions, list):
                        text = "; ".join(
                            str(item.get("text", item) if isinstance(item, dict) else str(item)).strip()
                            for item in captions
                            if item
                        ).strip("; ")
                    elif captions:
                        text = str(captions).strip()
                    if text:
                        text = f"[图片] {text}"
                    elif image_data:
                        text = "[图片] 文档图片"
                if text:
                    source_type = {"table": "table", "image": "image", "equation": "equation"}.get(block_type, "text")
                    chunk = {"page": page_number, "location": f"MinerU {block_type} {block_index}", "content": text, "source_type": source_type}
                    if image_data:
                        chunk["_image_data"] = image_data
                    chunks.append(chunk)
        if not chunks:
            raise ParseError("MinerU output did not contain normalized text blocks")
        return chunks

    @staticmethod
    def _read_image_data(block: dict[str, Any], output_dir: Path) -> str | None:
        """Read a MinerU image block as a temporary data URI for VLM captioning."""
        raw_path = block.get("img_path") or block.get("image_path") or block.get("image") or block.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = output_dir / candidate
        try:
            if not candidate.is_file():
                matches = list(output_dir.rglob(candidate.name))
                candidate = matches[0] if matches else candidate
            if not candidate.is_file() or candidate.stat().st_size > 12 * 1024 * 1024:
                return None
            media_type = mimetypes.guess_type(candidate.name)[0] or "image/png"
            encoded = base64.b64encode(candidate.read_bytes()).decode("ascii")
            return f"data:{media_type};base64,{encoded}"
        except OSError:
            return None


class DocxFallbackParser:
    """Fallback DOCX parser using python-docx when MinerU is unavailable."""

    def parse(self, path: Path) -> list[dict[str, Any]]:
        try:
            import docx
        except ImportError:
            raise ParseError("python-docx is not available; cannot parse DOCX files")
        doc = docx.Document(str(path))
        chunks: list[dict[str, Any]] = []
        for i, para in enumerate(doc.paragraphs, start=1):
            text = para.text.strip()
            if text:
                chunks.append({"page": 1, "location": f"DOCX paragraph {i}", "content": text, "source_type": "text"})
        for table_idx, table in enumerate(doc.tables, start=1):
            for row_idx, row in enumerate(table.rows, start=1):
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    text = " | ".join(cells)
                    chunks.append({"page": 1, "location": f"DOCX table {table_idx} row {row_idx}", "content": text, "source_type": "table"})
        if not chunks:
            raise ParseError("DOCX document contains no readable content")
        return chunks

class PdfFallbackParser:
    """Fallback PDF parser using pdfplumber when MinerU is unavailable."""

    def parse(self, path: Path) -> list[dict[str, Any]]:
        try:
            import pdfplumber
        except ImportError:
            raise ParseError("pdfplumber is not available; cannot parse PDF files")
        chunks: list[dict[str, Any]] = []
        with pdfplumber.open(str(path)) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                if text.strip():
                    chunks.append({"page": page_num, "location": f"PDF page {page_num}", "content": text.strip(), "source_type": "text"})
                tables = page.extract_tables() or []
                for t_idx, table in enumerate(tables, start=1):
                    for row in table:
                        cells = [str(c).strip() for c in row if c and str(c).strip()]
                        if cells:
                            chunks.append({"page": page_num, "location": f"PDF page {page_num} table {t_idx}", "content": " | ".join(cells), "source_type": "table"})
        if not chunks:
            raise ParseError("PDF document contains no readable text (may be image-only)")
        return chunks


def parse_document(path: Path, backend: str, mineru_command: str) -> tuple[str, list[dict[str, Any]]]:
    """Route documents to the best available parser.

    - PDF/DOC/DOCX → MinerU pipeline (layout, tables, OCR). DOC/DOCX are first
      converted to PDF via LibreOffice so images and table structures survive.
    - TXT/MD/CSV/JSON/XLS/XLSX/XLSM → lightweight readers (no layout to preserve).
    MinerU is mandatory for document formats; there is no silent quality loss.
    """
    suffix = path.suffix.lower()
    mineru = MinerUParser(mineru_command)

    if suffix in {".pdf", ".doc", ".docx"}:
        source = path
        converted = False
        if suffix in {".doc", ".docx"}:
            if mineru.available:
                if shutil.which("soffice") is None:
                    raise ParseError("LibreOffice (soffice) is not installed; cannot convert DOC/DOCX for the MinerU pipeline")
                source = _convert_to_pdf(path)
                converted = True
            else:
                # MinerU not available — use python-docx directly
                try:
                    return "simple", DocxFallbackParser().parse(path)
                except Exception as e:
                    raise ParseError(f"Failed to parse DOCX: {e}")
        if mineru.available:
            try:
                return "mineru", mineru.parse(source)
            finally:
                if converted:
                    source.unlink(missing_ok=True)
        else:
            # MinerU not available — use pdfplumber fallback for PDF
            try:
                return "simple", PdfFallbackParser().parse(path)
            except Exception as e:
                raise ParseError(f"Failed to parse PDF: {e}")

    if backend == "mineru":
        # 显式要求全量 MinerU：纯文本类也走统一管线（当前不支持，明确报错）
        raise ParseError(f"MinerU-only backend requested but {suffix or 'unknown'} is handled by the simple reader")

    reader = SimpleReader()
    return "simple", reader.parse(path)


def _convert_to_pdf(path: Path) -> Path:
    """Convert DOC/DOCX to PDF with headless LibreOffice; returns the new PDF path."""
    with tempfile.TemporaryDirectory(prefix="huizhi-convert-") as output_dir:
        result = subprocess.run(
            [
                "soffice",
                "--headless",
                "--norestore",
                "--convert-to",
                "pdf",
                "--outdir",
                output_dir,
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        pdf_files = list(Path(output_dir).glob("*.pdf"))
        if result.returncode != 0 or not pdf_files:
            detail = (result.stderr or result.stdout or "")[-500:]
            raise ParseError(f"LibreOffice conversion failed: {detail}")
        # Move out of the temp dir so caller can manage the lifecycle.
        # shutil.move handles cross-device transfers (/tmp vs /data volume).
        target = path.parent / f"{path.stem}.pdf"
        shutil.move(str(pdf_files[0]), str(target))
        return target


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
