"""Chat-driven table creation and import.

Approval is hard-disabled in this deployment, so the tool body carries the
authorization: it only reaches tables owned by the calling account, only reads
files inside that account's workspace, and never hands the user's attachment to
the importer directly (the importer deletes its source file on success).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from qwenpaw.agents.tools import data_tables, user_files

CSV = "name,amount\n甲,1\n乙,2\n"
OWNED_TABLE = {"id": "t-1", "table_name": "tb_hzxzs_sales_0001"}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Fake the collaborators and point the tool at a temporary workspace."""
    monkeypatch.delenv("CONSOLE_MEDIA_DIR", raising=False)
    monkeypatch.setattr(user_files, "get_current_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(user_files, "resolve_account_username", lambda: "u1")
    monkeypatch.setattr(data_tables, "_resolve_username", lambda: "u1")
    monkeypatch.setattr(
        data_tables,
        "_job_status_sync",
        lambda job_id: {
            "status": "completed",
            "total_rows": 2,
            "success_rows": 2,
            "failed_rows": 0,
            "errors": [],
        },
    )
    staging = tmp_path / "staging"
    monkeypatch.setattr("qwenpaw.data_tables.store.UPLOAD_DIR", str(staging))
    media = tmp_path / "media"
    media.mkdir()
    return media


@pytest.fixture
def importer(monkeypatch):
    """Replace the pandas-backed importer with a recorder.

    The real importer deletes its source file once the import succeeds, so the
    stub does too — that is what makes the copy worth asserting.
    """
    calls: list[tuple[str, str, str]] = []

    async def start_import_task(table_id: str, file_path: str, filename: str) -> str:
        calls.append((table_id, file_path, filename))
        Path(file_path).unlink(missing_ok=True)
        return "job-1"

    module = types.ModuleType("qwenpaw.data_tables.importer")
    module.start_import_task = start_import_task
    monkeypatch.setitem(sys.modules, "qwenpaw.data_tables.importer", module)
    return calls


def _write(path: Path, text: str = CSV) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


async def test_create_data_table_reports_the_generated_name(monkeypatch):
    monkeypatch.setattr(
        data_tables,
        "_create_table_sync",
        lambda username, description, display_name: {
            "id": "1",
            "table_name": "tb_hzxzs_sales_0001",
        },
    )

    text = await data_tables.create_data_table("2024 年销售明细")

    assert "tb_hzxzs_sales_0001" in text
    assert "import_data_table" in text


async def test_create_data_table_requires_a_description(monkeypatch):
    called = []
    monkeypatch.setattr(
        data_tables,
        "_create_table_sync",
        lambda *args: called.append(args),
    )

    text = await data_tables.create_data_table("   ")

    assert "描述" in text
    assert called == []


async def test_import_rejects_a_table_the_caller_does_not_own(workspace, monkeypatch):
    monkeypatch.setattr(data_tables, "_find_table_sync", lambda username, ref: None)

    text = await data_tables.import_data_table("tb_someone_else", "订单.csv")

    assert "没有找到属于你的数据表" in text


async def test_import_rejects_a_file_outside_the_workspace(
    workspace,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(data_tables, "_find_table_sync", lambda username, ref: OWNED_TABLE)
    outside = _write(tmp_path.parent / "another-account" / "订单.csv")

    text = await data_tables.import_data_table("tb_hzxzs_sales_0001", str(outside))

    assert "不在你的工作区内" in text


async def test_import_rejects_an_unsupported_extension(workspace, monkeypatch):
    monkeypatch.setattr(data_tables, "_find_table_sync", lambda username, ref: OWNED_TABLE)
    _write(workspace / "abc_备注.txt", "hello")

    text = await data_tables.import_data_table("tb_hzxzs_sales_0001", "备注.txt")

    assert "不支持的文件格式" in text


async def test_import_rejects_oversized_legacy_xls(workspace, monkeypatch):
    monkeypatch.setattr(data_tables, "_find_table_sync", lambda username, ref: OWNED_TABLE)
    monkeypatch.setattr("qwenpaw.data_tables.store.MAX_XLS_FILE_BYTES", 8)
    _write(workspace / "abc_老表.xls", "way more than eight bytes")

    text = await data_tables.import_data_table("tb_hzxzs_sales_0001", "老表.xls")

    assert "512MB" in text


async def test_import_copies_the_attachment_instead_of_consuming_it(
    workspace,
    importer,
    monkeypatch,
):
    """The importer deletes its source, so the chat upload must be copied."""
    monkeypatch.setattr(data_tables, "_find_table_sync", lambda username, ref: OWNED_TABLE)
    upload = _write(workspace / f"{'a' * 32}_销售数据.csv")

    text = await data_tables.import_data_table("tb_hzxzs_sales_0001", "销售数据.csv")

    assert upload.exists(), "the user's attachment must survive the import"
    table_id, staged_path, filename = importer[0]
    assert table_id == "t-1"
    assert staged_path != str(upload)
    # The stub mimicked the real importer and deleted the staged copy.
    assert not Path(staged_path).exists()
    # The uuid prefix is stripped so the import job list stays readable.
    assert filename == "销售数据.csv"
    assert "销售数据.csv" in text
    assert "成功 2 行" in text


async def test_import_reports_partial_failures(workspace, importer, monkeypatch):
    monkeypatch.setattr(data_tables, "_find_table_sync", lambda username, ref: OWNED_TABLE)
    monkeypatch.setattr(
        data_tables,
        "_job_status_sync",
        lambda job_id: {
            "status": "partial",
            "total_rows": 5,
            "success_rows": 3,
            "failed_rows": 2,
            "errors": [{"row": 4, "error": "invalid input syntax for type numeric"}],
        },
    )
    _write(workspace / f"{'b' * 32}_订单.csv")

    text = await data_tables.import_data_table("tb_hzxzs_sales_0001", "订单.csv")

    assert "失败 2 行" in text
    assert "invalid input syntax" in text


async def test_import_reports_failure_reason(workspace, importer, monkeypatch):
    monkeypatch.setattr(data_tables, "_find_table_sync", lambda username, ref: OWNED_TABLE)
    monkeypatch.setattr(
        data_tables,
        "_job_status_sync",
        lambda job_id: {
            "status": "failed",
            "total_rows": 0,
            "success_rows": 0,
            "failed_rows": 0,
            "errors": [{"error": "CSV 解析失败"}],
        },
    )
    _write(workspace / f"{'c' * 32}_坏文件.csv")

    text = await data_tables.import_data_table("tb_hzxzs_sales_0001", "坏文件.csv")

    assert "导入失败" in text
    assert "CSV 解析失败" in text


async def test_import_hands_back_the_job_id_when_still_running(
    workspace,
    importer,
    monkeypatch,
):
    monkeypatch.setattr(data_tables, "_find_table_sync", lambda username, ref: OWNED_TABLE)
    monkeypatch.setattr(data_tables, "_IMPORT_POLL_TIMEOUT", 0.05)
    monkeypatch.setattr(data_tables, "_IMPORT_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(
        data_tables,
        "_job_status_sync",
        lambda job_id: {"status": "loading"},
    )
    _write(workspace / f"{'d' * 32}_大表.csv")

    text = await data_tables.import_data_table("tb_hzxzs_sales_0001", "大表.csv")

    assert "job-1" in text
    assert "describe_data_table" in text
