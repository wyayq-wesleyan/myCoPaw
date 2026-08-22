from __future__ import annotations

import csv
import importlib.util
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class ParseError(RuntimeError):
    pass


def _table_to_text(rows: list[list[Any]], title: str) -> str:
    lines = [title]
    for row in rows:
        values = [str(value).strip() for value in row if value is not None and str(value).strip()]
        if values:
            lines.append(" | ".join(values))
    return "\n".join(lines)


class LocalParser:
    """Small, dependency-light parser used when MinerU is not installed yet."""

    def parse(self, path: Path) -> list[dict[str, Any]]:
        suffix = path.suffix.lower()
        if suffix in {".txt", ".md", ".csv"}:
            return self._parse_text(path, suffix)
        if suffix == ".pdf":
            return self._parse_pdf(path)
        if suffix == ".docx":
            return self._parse_docx(path)
        if suffix in {".xlsx", ".xlsm"}:
            return self._parse_xlsx(path)
        if suffix == ".doc":
            raise ParseError("Legacy .doc requires MinerU or LibreOffice conversion; please enable MinerU in the knowledge image.")
        raise ParseError(f"Unsupported file type: {suffix or 'unknown'}")

    def _parse_text(self, path: Path, suffix: str) -> list[dict[str, Any]]:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        if suffix == ".csv":
            with path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
                rows = list(csv.reader(handle))
            content = _table_to_text(rows, "CSV 表格")
        return [{"page": 1, "location": "正文", "content": content, "source_type": "text"}]

    def _parse_pdf(self, path: Path) -> list[dict[str, Any]]:
        try:
            import fitz  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ParseError("PyMuPDF is not installed in the knowledge service image") from exc
        chunks: list[dict[str, Any]] = []
        document = fitz.open(path)
        for page_index, page in enumerate(document, start=1):
            text = page.get_text("text").strip()
            if text:
                chunks.append({"page": page_index, "location": "正文", "content": text, "source_type": "text"})
            for table_index, table in enumerate(page.find_tables().tables, start=1):
                rows = table.extract()
                table_text = _table_to_text(rows, f"第 {table_index} 个表格")
                if table_text:
                    chunks.append({"page": page_index, "location": f"表格 {table_index}", "content": table_text, "source_type": "table"})
        document.close()
        if not chunks:
            raise ParseError("No extractable text found. Enable MinerU for OCR of scanned PDFs.")
        return chunks

    def _parse_docx(self, path: Path) -> list[dict[str, Any]]:
        try:
            from docx import Document  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ParseError("python-docx is not installed in the knowledge service image") from exc
        document = Document(path)
        chunks: list[dict[str, Any]] = []
        text = "\n".join(paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip())
        if text:
            chunks.append({"page": 1, "location": "正文", "content": text, "source_type": "text"})
        for table_index, table in enumerate(document.tables, start=1):
            rows = [[cell.text for cell in row.cells] for row in table.rows]
            table_text = _table_to_text(rows, f"第 {table_index} 个表格")
            if table_text:
                chunks.append({"page": 1, "location": f"表格 {table_index}", "content": table_text, "source_type": "table"})
        if not chunks:
            raise ParseError("The DOCX file does not contain extractable text or tables")
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
                chunks.append({"page": 1, "location": f"工作表：{sheet.title}", "content": content, "source_type": "table"})
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
                if text:
                    chunks.append({"page": page_index, "location": f"MinerU {block_type} {block_index}", "content": text, "source_type": "table" if block_type == "table" else "text"})
        if not chunks:
            raise ParseError("MinerU output did not contain normalized text blocks")
        return chunks


def parse_document(path: Path, backend: str, mineru_command: str) -> tuple[str, list[dict[str, Any]]]:
    mineru = MinerUParser(mineru_command)
    if backend == "mineru" or (backend == "auto" and mineru.available):
        return "mineru", mineru.parse(path)
    if backend not in {"auto", "local"}:
        raise ParseError(f"Unsupported parser backend: {backend}")
    return "local", LocalParser().parse(path)
