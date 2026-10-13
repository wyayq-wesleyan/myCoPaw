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
        lambda username, description, display_name, path_id="": {
            "id": "1",
            "table_name": "tb_hzxzs_sales_0001",
        },
    )

    text = await data_tables.create_data_table("2024 年销售明细")

    assert "tb_hzxzs_sales_0001" in text
    assert "import_data_table" in text
    # No case named -> the table stays in the account's default area.
    assert "默认区" in text


async def test_create_data_table_passes_the_resolved_path_through(monkeypatch):
    """指定案件时，解析出的 path_id 必须真的传给建表函数。

    这条锁定的是「智能体只能把数据放到默认路径」这个缺口：``create_table_record``
    一直支持 ``path_id``，是工具层没把它传下去，所以建出来的表永远落在公共区。
    """
    import qwenpaw.agents.tools.path_scope as path_scope

    seen: list[tuple] = []
    monkeypatch.setattr(path_scope, "resolve_writable_path_id", lambda u, ref: "p-123")
    monkeypatch.setattr(
        data_tables,
        "_create_table_sync",
        lambda username, description, display_name, path_id="": (
            seen.append((username, description, display_name, path_id))
            or {"id": "1", "table_name": "tb_hzxzs_case_0001"}
        ),
    )
    monkeypatch.setattr(data_tables, "_describe_path_label", lambda u, p: "案件A")

    text = await data_tables.create_data_table("案件材料", path="案件A")

    assert seen and seen[0][3] == "p-123", f"path_id not forwarded: {seen}"
    assert "案件A" in text


async def test_create_data_table_refuses_an_ambiguous_path(monkeypatch):
    """重名时不猜：把候选还给模型，让用户确认。"""
    import qwenpaw.agents.tools.path_scope as path_scope

    created: list = []
    monkeypatch.setattr(
        path_scope,
        "resolve_writable_path_id",
        lambda u, ref: (_ for _ in ()).throw(
            path_scope.PathAmbiguous([
                {"id": "p-1", "name": "案件A"},
                {"id": "p-2", "name": "案件A"},
            ]),
        ),
    )
    monkeypatch.setattr(
        data_tables, "_create_table_sync",
        lambda *a, **k: created.append(a) or {"id": "1", "table_name": "t"},
    )

    text = await data_tables.create_data_table("材料", path="案件A")

    assert "多个路径" in text
    assert "p-1" in text and "p-2" in text
    assert created == [], "must not create a table when the case is ambiguous"


async def test_create_data_table_reports_an_ungranted_path(monkeypatch):
    """未授权的案件名必须被拒绝，而不是悄悄落到默认区。"""
    import qwenpaw.agents.tools.path_scope as path_scope

    created: list = []
    monkeypatch.setattr(
        path_scope,
        "resolve_writable_path_id",
        lambda u, ref: (_ for _ in ()).throw(
            path_scope.PathNotWritable("没有找到名为「别人的案件」的可写入路径。"),
        ),
    )
    monkeypatch.setattr(
        data_tables, "_create_table_sync",
        lambda *a, **k: created.append(a) or {"id": "1", "table_name": "t"},
    )

    text = await data_tables.create_data_table("材料", path="别人的案件")

    assert "别人的案件" in text
    assert created == [], "must not create a table outside the granted paths"


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


# ---------------------------------------------------------------------------
# 归入案件
# ---------------------------------------------------------------------------


async def test_move_files_a_table_into_a_case(monkeypatch):
    """把已有表移入案件：解析路径 → 先搬后绑，并把结果告诉模型。"""
    import qwenpaw.agents.tools.path_scope as path_scope

    monkeypatch.setattr(
        path_scope, "resolve_writable_path_id", lambda u, ref: "case-A",
    )
    monkeypatch.setattr(
        path_scope, "visible_data_path_ids", lambda u: {"root-1", "case-A"},
    )
    monkeypatch.setattr(path_scope, "root_path_id", lambda u: "root-1")
    monkeypatch.setattr(
        data_tables, "_find_table_sync",
        lambda u, ref: {"id": "t-1", "table_name": "tb_x", "path_id": ""},
    )
    moved: list[tuple] = []
    monkeypatch.setattr(
        data_tables, "_move_table_sync",
        lambda table_id, path_id: moved.append((table_id, path_id))
        or {"from": "public", "to": "p_casea", "moved": "true"},
    )
    monkeypatch.setattr(data_tables, "_describe_path_label", lambda u, p: "案件A")

    text = await data_tables.move_data_table_to_path("tb_x", path="案件A")

    assert moved == [("t-1", "case-A")], f"未按预期移动: {moved}"
    assert "案件A" in text


async def test_move_into_the_default_area_unbinds(monkeypatch):
    """path 留空 = 移回默认区，必须真的解绑而不是留在原案件。"""
    import qwenpaw.agents.tools.path_scope as path_scope

    monkeypatch.setattr(path_scope, "resolve_writable_path_id", lambda u, ref: "")
    monkeypatch.setattr(
        path_scope, "visible_data_path_ids", lambda u: {"root-1", "case-A"},
    )
    monkeypatch.setattr(path_scope, "root_path_id", lambda u: "root-1")
    monkeypatch.setattr(
        data_tables, "_find_table_sync",
        lambda u, ref: {"id": "t-1", "table_name": "tb_x", "path_id": "case-A"},
    )
    moved: list[tuple] = []
    monkeypatch.setattr(
        data_tables, "_move_table_sync",
        lambda table_id, path_id: moved.append((table_id, path_id))
        or {"from": "p_casea", "to": "public", "moved": "true"},
    )

    text = await data_tables.move_data_table_to_path("tb_x")

    assert moved == [("t-1", "")], f"未移回默认区: {moved}"
    assert "默认区" in text


async def test_move_refuses_a_table_outside_the_granted_paths(monkeypatch):
    """不能移动该智能体看不到的表 —— 那是往没授权的案件里写。"""
    import qwenpaw.agents.tools.path_scope as path_scope

    monkeypatch.setattr(
        path_scope, "resolve_writable_path_id", lambda u, ref: "case-A",
    )
    monkeypatch.setattr(
        path_scope, "visible_data_path_ids", lambda u: {"case-A"},
    )
    monkeypatch.setattr(path_scope, "root_path_id", lambda u: "root-1")
    monkeypatch.setattr(
        data_tables, "_find_table_sync",
        lambda u, ref: {"id": "t-9", "table_name": "tb_other", "path_id": "case-OTHER"},
    )
    moved: list = []
    monkeypatch.setattr(
        data_tables, "_move_table_sync",
        lambda *a: moved.append(a) or {"moved": "true"},
    )

    text = await data_tables.move_data_table_to_path("tb_other", path="案件A")

    assert "无法移动" in text or "可访问" in text
    assert moved == [], "越权表不得被移动"


async def test_move_reports_an_unknown_table(monkeypatch):
    import qwenpaw.agents.tools.path_scope as path_scope

    monkeypatch.setattr(path_scope, "resolve_writable_path_id", lambda u, ref: "")
    monkeypatch.setattr(data_tables, "_find_table_sync", lambda u, ref: None)

    text = await data_tables.move_data_table_to_path("不存在的表")

    assert "没有找到名为" in text


async def test_move_notes_an_unimported_table(monkeypatch):
    """未导入的表没有物理表可搬，要说明清楚而不是让用户以为失败了。"""
    import qwenpaw.agents.tools.path_scope as path_scope

    monkeypatch.setattr(
        path_scope, "resolve_writable_path_id", lambda u, ref: "case-A",
    )
    monkeypatch.setattr(
        path_scope, "visible_data_path_ids", lambda u: {"root-1", "case-A"},
    )
    monkeypatch.setattr(path_scope, "root_path_id", lambda u: "root-1")
    monkeypatch.setattr(
        data_tables, "_find_table_sync",
        lambda u, ref: {"id": "t-1", "table_name": "tb_x", "path_id": ""},
    )
    monkeypatch.setattr(
        data_tables, "_move_table_sync",
        lambda *a: {"from": "", "to": "p_casea", "moved": "false"},
    )
    monkeypatch.setattr(data_tables, "_describe_path_label", lambda u, p: "案件A")

    text = await data_tables.move_data_table_to_path("tb_x", path="案件A")

    assert "尚未导入" in text
    assert "案件A" in text


async def test_list_paths_shows_granted_cases(monkeypatch):
    """list_paths 是模型发现案件名的唯一入口。"""
    import qwenpaw.agents.tools.path_scope as path_scope

    monkeypatch.setattr(
        path_scope, "granted_path_nodes",
        lambda u: [
            {"id": "root-1", "name": "默认", "is_root": True, "parent_id": ""},
            {"id": "case-A", "name": "案件A", "is_root": False, "parent_id": "root-1"},
        ],
    )

    text = data_tables.list_paths()

    assert "案件A" in text
    assert "默认区" in text
    assert "path" in text  # tells the model which argument to use


async def test_list_paths_explains_no_access(monkeypatch):
    """没有授权时要说明原因，而不是回一个空列表让模型猜。"""
    import qwenpaw.agents.tools.path_scope as path_scope

    monkeypatch.setattr(path_scope, "granted_path_nodes", lambda u: None)

    text = data_tables.list_paths()

    assert "没有可访问的路径" in text
    assert "数据分析" in text


async def test_create_data_table_reaches_the_real_recorder_with_the_path(monkeypatch):
    """不 stub ``_create_table_sync``，而是替换它真正调用的 ``create_table_record``。

    上面的 ``test_create_data_table_passes_the_resolved_path_through`` 把
    ``_create_table_sync`` 整个换掉了，所以「工具确实把 path_id 传下去」这一环
    其实是靠约定而非断言保证的 —— 把 ``path_id=path_id`` 删掉那个测试依然通过。
    这条测试盯住真正的调用点，回退会精确失败。
    """
    import sys
    import types

    import qwenpaw.agents.tools.path_scope as path_scope

    seen: list[dict] = []

    def create_table_record(username, display_name="", description="",
                            columns=None, column_types=None, path_id=""):
        seen.append({"username": username, "path_id": path_id})
        return {"id": "1", "table_name": "tb_hzxzs_case_0001"}

    module = types.ModuleType("qwenpaw.data_tables.table_api")
    module.create_table_record = create_table_record
    monkeypatch.setitem(sys.modules, "qwenpaw.data_tables.table_api", module)
    monkeypatch.setattr(
        path_scope, "resolve_writable_path_id", lambda u, ref: "case-A",
    )
    monkeypatch.setattr(data_tables, "_resolve_username", lambda: "u1")
    monkeypatch.setattr(data_tables, "_describe_path_label", lambda u, p: "案件A")

    await data_tables.create_data_table("案件材料", path="案件A")

    assert seen, "create_table_record was never called"
    assert seen[0]["path_id"] == "case-A", (
        f"path_id did not reach create_table_record: {seen}"
    )


async def test_create_data_table_defaults_to_no_path(monkeypatch):
    """未指定案件时传空 path_id —— 落到默认区，不能凭空挑一个案件。"""
    import sys
    import types

    import qwenpaw.agents.tools.path_scope as path_scope

    seen: list[dict] = []

    def create_table_record(username, display_name="", description="",
                            columns=None, column_types=None, path_id=""):
        seen.append({"path_id": path_id})
        return {"id": "1", "table_name": "tb_x"}

    module = types.ModuleType("qwenpaw.data_tables.table_api")
    module.create_table_record = create_table_record
    monkeypatch.setitem(sys.modules, "qwenpaw.data_tables.table_api", module)
    monkeypatch.setattr(
        path_scope, "resolve_writable_path_id", lambda u, ref: "",
    )
    monkeypatch.setattr(data_tables, "_resolve_username", lambda: "u1")

    await data_tables.create_data_table("普通表")

    assert seen == [{"path_id": ""}], f"unexpected path: {seen}"
