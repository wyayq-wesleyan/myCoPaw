from __future__ import annotations

import base64
import csv
import importlib.util
import json
import mimetypes
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import logging
import zipfile
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


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

    def __init__(self, command: str, timeout_seconds: int = 900) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds

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
            # 模板来自 MINERU_COMMAND，以 shell=True 执行，因此路径必须按 shell
            # 规则转义：文件名里的 ``$(...)``、反引号、分号在双引号内仍会被解释。
            # 调用方已用 _safe_filename 清洗过，这里是第二道防线——MINERU_COMMAND
            # 也可以被改成不带引号的模板。
            command = self.command.format(
                input=shlex.quote(str(path)),
                output=shlex.quote(output_dir),
            )
            try:
                result = _run_child(
                    command,
                    shell=True,
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise ParseError(
                    f"MinerU timed out after {self.timeout_seconds}s "
                    "(the process tree was terminated)",
                ) from exc

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


def parse_document(
    path: Path,
    backend: str,
    mineru_command: str,
    *,
    offline: bool = False,
    mineru_timeout_seconds: int = 900,
) -> tuple[str, list[dict[str, Any]]]:
    """Route documents to the best available parser.

    - PDF/DOC/DOCX → MinerU pipeline (layout, tables, OCR) when available.
      In auto mode, a failed local MinerU run falls back to pdfplumber/python-docx
      so an offline model download cannot mark an otherwise readable document as
      failed. DOC/DOCX are first converted to PDF via LibreOffice for MinerU.
    - TXT/MD/CSV/JSON/XLS/XLSX/XLSM → lightweight readers (no layout to preserve).
    `backend=mineru` remains strict; `backend=auto` is resilient for offline
    deployments.
    """
    suffix = path.suffix.lower()
    mineru = MinerUParser(mineru_command, mineru_timeout_seconds)
    use_mineru = mineru.available and not offline and backend in {"auto", "mineru"}
    if suffix in {".pdf", ".doc", ".docx"} and backend == "mineru" and not use_mineru:
        reason = "offline mode is enabled" if offline else "MinerU is unavailable"
        raise ParseError(f"MinerU-only backend requested but {reason}; preload MinerU models or use PARSER_BACKEND=auto")

    if suffix in {".pdf", ".doc", ".docx"}:
        source = path
        converted = False
        if suffix in {".doc", ".docx"}:
            if use_mineru:
                if shutil.which("soffice") is None:
                    raise ParseError("LibreOffice (soffice) is not installed; cannot convert DOC/DOCX for the MinerU pipeline")
                source = _convert_to_pdf(path)
                converted = True
            else:
                # MinerU is unavailable or explicitly disabled: use the
                # dependency-light DOCX reader. Legacy .doc files need a
                # LibreOffice conversion and cannot be parsed by python-docx.
                try:
                    if suffix == ".doc":
                        raise ParseError("旧版 .doc 在离线模式下需要 LibreOffice 转换；请安装 soffice 或上传 .docx")
                    chunks = DocxFallbackParser().parse(path)
                    logger.info("Parsed %s with python-docx fallback (%d chunks)", path.name, len(chunks))
                    return "simple", chunks
                except Exception as e:
                    raise ParseError(f"Failed to parse DOCX: {e}")
        if use_mineru:
            mineru_succeeded = False
            try:
                chunks = mineru.parse(source)
                mineru_succeeded = True
                return "mineru", chunks
            except Exception as exc:
                if backend != "auto":
                    raise
                logger.warning(
                    "MinerU failed for %s; falling back to pdfplumber/python-docx: %s",
                    path.name,
                    exc,
                )
            finally:
                if converted and (mineru_succeeded or suffix != ".doc"):
                    source.unlink(missing_ok=True)
        # MinerU is unavailable, disabled, or failed — use pdfplumber fallback.
        try:
            if suffix == ".docx":
                chunks = DocxFallbackParser().parse(path)
                fallback_name = "python-docx"
            else:
                fallback_path = source if converted and source.exists() else path
                chunks = PdfFallbackParser().parse(fallback_path)
                fallback_name = "pdfplumber"
            logger.info("Parsed %s with %s fallback (%d chunks)", path.name, fallback_name, len(chunks))
            return "simple", chunks
        except Exception as e:
            raise ParseError(
                f"Failed to parse PDF: {e}. This may be an image-only/scanned PDF; "
                "offline OCR requires preloaded MinerU/OCR models."
            ) from e
        finally:
            if converted:
                source.unlink(missing_ok=True)

    if backend == "mineru":
        # 显式要求全量 MinerU：纯文本类也走统一管线（当前不支持，明确报错）
        raise ParseError(f"MinerU-only backend requested but {suffix or 'unknown'} is handled by the simple reader")

    reader = SimpleReader()
    return "simple", reader.parse(path)


def _kill_process_group(process: subprocess.Popen) -> None:
    """Kill *process* together with everything it forked.

    ``subprocess.run(timeout=...)`` kills only the direct child. Both callers
    here start a tree: ``soffice`` is a shell wrapper that execs ``soffice.bin``
    (which itself forks), and MinerU runs under ``shell=True`` so the direct
    child is only ``/bin/sh``. On timeout the real workers kept running.

    That is how a few stuck conversions exhausted the container's PID budget:
    once ``pids.max`` is reached, every further ``fork`` fails — visible as
    ``soffice: Cannot fork`` — and ``asyncio.to_thread`` fails with
    ``can't start new thread``, which took the whole document-list endpoint down
    for minutes until the orphans exited on their own.
    """
    if os.name != "posix":
        process.kill()
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Already gone, or the group is not ours to signal.
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _cpu_budget() -> int:
    """How many CPUs this process may actually use.

    ``os.cpu_count()`` reports the **host's** core count inside a container:
    ``cpus: 4`` in Compose is a CFS quota, not an affinity mask, so the process
    still sees 256. Libraries that size their thread pools from it (PyTorch /
    OpenMP / MKL, all of which MinerU pulls in) then start one worker per host
    core — 256 threads for a container allowed to run 4 cores' worth of work.

    Reads the cgroup quota where available and falls back to the affinity mask,
    then to ``os.cpu_count()``.
    """
    # cgroup v2
    try:
        with open("/sys/fs/cgroup/cpu.max", encoding="utf-8") as handle:
            quota, period = handle.read().split()[:2]
        if quota != "max" and int(period) > 0:
            return max(1, round(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    # cgroup v1
    try:
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", encoding="utf-8") as handle:
            quota = int(handle.read().strip())
        with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us", encoding="utf-8") as handle:
            period = int(handle.read().strip())
        if quota > 0 and period > 0:
            return max(1, round(quota / period))
    except (OSError, ValueError):
        pass
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def _child_env() -> dict[str, str]:
    """Environment for parser subprocesses, with thread pools sized to the quota.

    MinerU imports PyTorch. With no explicit limit PyTorch starts
    ``os.cpu_count()`` intra-op threads — on this deployment 256 per process,
    against a 4-CPU quota. A handful of concurrent parses then exhausts the
    container's PID budget (threads are tasks too), after which every ``fork``
    fails: that is the ``soffice: Cannot fork`` /
    ``RuntimeError: can't start new thread`` cascade.

    Capping here rather than only in Compose keeps it correct for anyone who
    runs the service outside Docker too.
    """
    env = dict(os.environ)
    budget = _cpu_budget()
    # An explicit operator setting wins: they may know the workload better.
    for name in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        env.setdefault(name, str(budget))
    return env


def _run_child(
    command: str | list[str],
    *,
    shell: bool,
    timeout: float,
) -> subprocess.CompletedProcess:
    """Run a child process in its own session, killing the whole tree on timeout.

    ``start_new_session=True`` puts the child in a new process group so
    ``_kill_process_group`` can reach its descendants. Thread pools are capped
    via ``_child_env`` so one parse cannot claim the whole host's cores. Used by
    both the LibreOffice and MinerU calls — see ``_kill_process_group`` for why
    that matters.
    """
    process = subprocess.Popen(
        command,
        shell=shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=_child_env(),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        try:
            # Reap the corpse so the PID is released before we return.
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("child process %s survived SIGKILL", process.pid)
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _convert_to_pdf(path: Path) -> Path:
    """Convert DOC/DOCX to PDF with headless LibreOffice; returns the new PDF path."""
    with tempfile.TemporaryDirectory(prefix="huizhi-convert-") as output_dir:
        try:
            result = _run_child(
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
                shell=False,
                timeout=300,
            )
        except subprocess.TimeoutExpired as exc:
            raise ParseError(
                "LibreOffice conversion timed out after 300s "
                "(the process tree was terminated)",
            ) from exc
        pdf_files = list(Path(output_dir).glob("*.pdf"))
        if result.returncode != 0 or not pdf_files:
            detail = (result.stderr or result.stdout or "")[-500:]
            raise ParseError(f"LibreOffice conversion failed: {detail}")
        # Move out of the temp dir so caller can manage the lifecycle.
        # shutil.move handles cross-device transfers (/tmp vs /data volume).
        target = path.parent / f"{path.stem}.pdf"
        shutil.move(str(pdf_files[0]), str(target))
        return target


def extract_text_from_file(
    path: Path,
    backend: str,
    mineru_command: str,
    *,
    offline: bool = False,
    mineru_timeout_seconds: int = 900,
) -> dict[str, Any]:
    """Parse a file and return plain text with metadata."""
    used_backend, chunks = parse_document(
        path,
        backend,
        mineru_command,
        offline=offline,
        mineru_timeout_seconds=mineru_timeout_seconds,
    )
    pages = set()
    parts = []
    images: list[str] = []
    for chunk in chunks:
        image_data = chunk.get("_image_data")
        if isinstance(image_data, str) and image_data.startswith("data:image/"):
            images.append(image_data)
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
    # Fallback readers do not have MinerU's image blocks. Extract embedded
    # DOCX media and PDF page images for chat's full-context path as well.
    if not images:
        images.extend(_extract_embedded_images(path))
    return {
        "content": "\n\n".join(parts),
        "backend": used_backend,
        "pages": len(pages) if pages else None,
        "truncated": False,
        "images": images,
    }


def _extract_embedded_images(path: Path) -> list[str]:
    """Return bounded data URIs for images embedded in PDF/DOCX files."""
    max_each = 12 * 1024 * 1024
    max_total = 20 * 1024 * 1024
    candidates: list[tuple[str, bytes]] = []
    suffix = path.suffix.lower()
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    if not name.startswith("word/media/"):
                        continue
                    raw = archive.read(name)
                    media_type = mimetypes.guess_type(name)[0] or "image/png"
                    candidates.append((media_type, raw))
        except (OSError, zipfile.BadZipFile):
            return []
    elif suffix == ".pdf":
        try:
            import fitz  # type: ignore[import-not-found]

            with fitz.open(str(path)) as document:
                for page in document:
                    for image in page.get_images(full=True):
                        raw = document.extract_image(image[0])
                        candidates.append((
                            raw.get("mime", "image/png"),
                            raw.get("image", b""),
                        ))
        except (ImportError, OSError, RuntimeError):
            return []

    output: list[str] = []
    total = 0
    for media_type, raw in candidates:
        if not raw or len(raw) > max_each or total + len(raw) > max_total:
            continue
        total += len(raw)
        output.append(
            f"data:{media_type};base64,{base64.b64encode(raw).decode('ascii')}"
        )
    return output
