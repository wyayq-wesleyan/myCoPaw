from __future__ import annotations

import csv
import importlib.util
import json
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
            raw = json.loads(json_files[0].read_text(encoding="utf-8"))
        return self._normalize(raw)

    def _normalize(self, raw: Any) -> list[dict[str, Any]]:
        if isinstance(raw, list):
            pages = raw
        elif isinstance(raw, dict) and isinstance(raw.get("pages"), list):
            pages = raw["pages"]
        else:
            raise ParseError("Unsupported MinerU JSON shape; configure an adapter for this MinerU release")
        chunks: list[dict[str, Any]] = []
        for page_index, page in enumerate(pages, start=1):
            if isinstance(page, dict) and page.get("type") in {"text", "table", "image", "equation"}:
                blocks = [page]
            elif isinstance(page, dict):
                blocks = page.get("blocks", page.get("content", []))
            else:
                blocks = []
            for block_index, block in enumerate(blocks, start=1):
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type", "text"))
                text = str(block.get("text", block.get("content", ""))).strip()
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
                if text:
                    source_type = {"table": "table", "image": "image", "equation": "equation"}.get(block_type, "text")
                    chunks.append({"page": page_index, "location": f"MinerU {block_type} {block_index}", "content": text, "source_type": source_type})
        if not chunks:
            raise ParseError("MinerU output did not contain normalized text blocks")
        return chunks


def parse_document(path: Path, backend: str, mineru_command: str) -> tuple[str, list[dict[str, Any]]]:
    """Route documents to the best available parser.

    - PDF/DOC/DOCX → MinerU pipeline (layout, tables, OCR). DOC/DOCX are first
      converted to PDF via LibreOffice so images and table structures survive.
    - TXT/MD/CSV/XLSX/XLSM → lightweight readers (no layout to preserve).
    MinerU is mandatory for document formats; there is no silent quality loss.
    """
    suffix = path.suffix.lower()
    mineru = MinerUParser(mineru_command)

    if suffix in {".pdf", ".doc", ".docx"}:
        source = path
        converted = False
        if suffix in {".doc", ".docx"}:
            if shutil.which("soffice") is None:
                raise ParseError("LibreOffice (soffice) is not installed; cannot convert DOC/DOCX for the MinerU pipeline")
            source = _convert_to_pdf(path)
            converted = True
        try:
            return "mineru", mineru.parse(source)
        finally:
            if converted:
                source.unlink(missing_ok=True)

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
