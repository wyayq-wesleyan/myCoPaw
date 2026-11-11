# -*- coding: utf-8 -*-
"""上传收件箱与压缩包解压的服务端测试。

覆盖两类风险：
1. 上传本身：文件名穿越、超过上限、sha256 与原子落盘；
2. 解压：路径穿越、符号链接、压缩炸弹、文件数上限、单顶层目录去重、
   失败时保留原压缩包且不留下半成品。
"""

from __future__ import annotations

import gzip
import io
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest
from starlette.datastructures import UploadFile

from qwenpaw.services import upload_inbox as inbox


def _upload(data: bytes, name: str = "a.txt") -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, payload in entries.items():
            bundle.writestr(name, payload)
    return path


# ---------------------------------------------------------------------------
# 文件名与目标分配
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["../x.txt", "..\\x.txt", "/etc/passwd", "a/b/c.txt"],
)
def test_safe_filename_strips_everything_but_the_basename(raw: str) -> None:
    result = inbox.safe_filename(raw)
    assert "/" not in result and "\\" not in result
    assert result not in (".", "..")


@pytest.mark.parametrize("raw", ["", None, ".", ".."])
def test_safe_filename_rejects_empty_or_dot_names(raw) -> None:
    with pytest.raises(inbox.InboxUploadError):
        inbox.safe_filename(raw)


def test_unique_target_keeps_compound_suffix(tmp_path: Path) -> None:
    (tmp_path / "data.tar.gz").write_bytes(b"x")
    target = inbox.unique_target(tmp_path, "data.tar.gz")
    assert target.name == "data (2).tar.gz"


def test_safe_filename_truncates_long_names_by_utf8_bytes() -> None:
    """A 300-character Chinese name is 900 bytes but the FS cap is 255."""
    result = inbox.safe_filename("数" * 300 + ".txt")

    assert len(result.encode("utf-8")) <= 200
    assert result.endswith(".txt")
    assert result.startswith("数")


def test_safe_filename_truncates_a_long_name_without_suffix() -> None:
    result = inbox.safe_filename("数" * 300)

    assert len(result.encode("utf-8")) <= 200
    assert result


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------


def test_save_upload_streams_and_hashes(tmp_path: Path) -> None:
    target = inbox._reserve_target(tmp_path, "a.txt")
    size, digest = inbox.save_upload(
        _upload(b"hello"),
        target,
        limit_bytes=1024,
    )
    assert size == 5
    assert target.read_bytes() == b"hello"
    assert len(digest) == 64


def test_save_upload_rejects_oversize_and_leaves_nothing(tmp_path: Path) -> None:
    target = inbox._reserve_target(tmp_path, "big.bin")
    with pytest.raises(inbox.InboxUploadError):
        inbox.save_upload(_upload(b"x" * 2048), target, limit_bytes=1024)
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# 解压
# ---------------------------------------------------------------------------


def test_extract_zip_normal(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "pkg.zip", {"a.txt": b"a", "dir/b.txt": b"b"})
    result = inbox.extract_archive(archive, tmp_path / "out")
    assert result["files"] == 2
    assert (tmp_path / "out" / "a.txt").read_bytes() == b"a"
    assert (tmp_path / "out" / "dir" / "b.txt").read_bytes() == b"b"


def test_extract_zip_flattens_a_single_root(tmp_path: Path) -> None:
    archive = _zip(
        tmp_path / "pkg.zip",
        {"data/a.txt": b"a", "data/b.txt": b"b"},
    )
    result = inbox.extract_archive(archive, tmp_path / "out")
    assert result["target"] == tmp_path / "out"
    assert (tmp_path / "out" / "a.txt").is_file()
    assert not (tmp_path / "out" / "data").exists()


def test_extract_zip_rejects_traversal(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "evil.zip", {"../escape.txt": b"x"})
    with pytest.raises(inbox.ArchiveError):
        inbox.extract_archive(archive, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_extract_zip_skips_symlinks(tmp_path: Path) -> None:
    archive = tmp_path / "links.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(info, "/etc/passwd")
        bundle.writestr("ok.txt", "ok")
    inbox.extract_archive(archive, tmp_path / "out")
    assert not (tmp_path / "out" / "link").exists()
    assert (tmp_path / "out" / "ok.txt").is_file()


def test_extract_zip_rejects_a_compression_bomb(tmp_path: Path) -> None:
    archive = _zip(
        tmp_path / "bomb.zip",
        {"big.bin": b"\x00" * (4 * 1024 * 1024)},
    )
    with pytest.raises(inbox.ArchiveError, match="压缩比"):
        inbox.extract_archive(archive, tmp_path / "out")


def test_extract_zip_respects_the_file_count_limit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(inbox, "_MAX_EXTRACT_FILES", 2)
    archive = _zip(
        tmp_path / "many.zip",
        {f"{i}.txt": b"x" for i in range(5)},
    )
    with pytest.raises(inbox.ArchiveError, match="文件数"):
        inbox.extract_archive(archive, tmp_path / "out")


def test_extract_tar_skips_symlinks(tmp_path: Path) -> None:
    archive = tmp_path / "pkg.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        data = b"ok"
        info = tarfile.TarInfo("ok.txt")
        info.size = len(data)
        bundle.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        bundle.addfile(link)
    inbox.extract_archive(archive, tmp_path / "out")
    assert (tmp_path / "out" / "ok.txt").read_bytes() == b"ok"
    assert not (tmp_path / "out" / "link").exists()


def test_extract_gz_single_file(tmp_path: Path) -> None:
    archive = tmp_path / "note.txt.gz"
    with gzip.open(archive, "wb") as handle:
        handle.write(b"hello")
    inbox.extract_archive(archive, tmp_path / "out")
    assert (tmp_path / "out" / "note.txt").read_bytes() == b"hello"


def test_extract_rejects_unsupported_format(tmp_path: Path) -> None:
    archive = tmp_path / "plain.bin"
    archive.write_bytes(b"not an archive")
    with pytest.raises(inbox.ArchiveError, match="不支持的压缩格式"):
        inbox.extract_archive(archive, tmp_path / "out")


# ---------------------------------------------------------------------------
# 任务状态
# ---------------------------------------------------------------------------


def test_job_becomes_ready(tmp_path: Path) -> None:
    _zip(tmp_path / "pkg.zip", {"a.txt": b"a"})
    workspace = tmp_path / "ws"
    workspace.mkdir()
    archive = workspace / "upload" / "pkg.zip"
    archive.parent.mkdir(parents=True)
    _zip(archive, {"a.txt": b"a"})

    job = inbox.create_extract_job(workspace, archive)
    inbox.run_extract_job(workspace, job["id"])

    stored = inbox.read_job(workspace, job["id"])
    assert stored is not None
    assert stored["status"] == "ready"
    assert stored["files"] == 1
    assert stored["archive_deleted"] is True
    assert (Path(stored["target"]) / "a.txt").is_file()
    assert not archive.exists(), "a successfully extracted archive is removed"


def test_extraction_never_deletes_an_archive_outside_the_inbox(
    tmp_path: Path,
) -> None:
    """Defense in depth: the job record cannot point deletion outside upload/."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = _zip(tmp_path / "outside.zip", {"a.txt": b"a"})

    job = inbox.create_extract_job(workspace, outside)
    inbox.run_extract_job(workspace, job["id"])

    assert outside.is_file()
    stored = inbox.read_job(workspace, job["id"])
    assert stored is not None
    assert stored["status"] == "ready"
    assert stored["archive_deleted"] is False


# ---------------------------------------------------------------------------
# 文件管理（仅 upload/ 内）
# ---------------------------------------------------------------------------


def test_rename_inbox_entry_moves_the_file(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    upload = workspace / "upload"
    upload.mkdir(parents=True)
    (upload / "a.txt").write_text("a", encoding="utf-8")

    path = inbox.rename_inbox_entry(workspace, "upload/a.txt", "b.txt")

    assert path == "upload/b.txt"
    assert (upload / "b.txt").read_text(encoding="utf-8") == "a"
    assert not (upload / "a.txt").exists()


def test_rename_inbox_entry_rejects_a_collision(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    upload = workspace / "upload"
    upload.mkdir(parents=True)
    (upload / "a.txt").write_text("a", encoding="utf-8")
    (upload / "b.txt").write_text("b", encoding="utf-8")

    with pytest.raises(inbox.InboxConflictError):
        inbox.rename_inbox_entry(workspace, "upload/a.txt", "b.txt")
    assert (upload / "a.txt").is_file()


@pytest.mark.parametrize(
    "new_name",
    ["", ".", "..", ".hidden", ".jobs", "a/b.txt", "a\\b.txt", "x" * 300],
)
def test_rename_inbox_entry_rejects_bad_names(
    tmp_path: Path,
    new_name: str,
) -> None:
    workspace = tmp_path / "ws"
    upload = workspace / "upload"
    upload.mkdir(parents=True)
    (upload / "a.txt").write_text("a", encoding="utf-8")

    with pytest.raises(inbox.InboxUploadError):
        inbox.rename_inbox_entry(workspace, "upload/a.txt", new_name)
    assert (upload / "a.txt").is_file()


def test_delete_inbox_entry_requires_recursive_for_directories(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    upload = workspace / "upload"
    (upload / "data").mkdir(parents=True)
    (upload / "data" / "a.txt").write_text("a", encoding="utf-8")

    with pytest.raises(inbox.InboxConflictError):
        inbox.delete_inbox_entry(workspace, "upload/data")
    inbox.delete_inbox_entry(workspace, "upload/data", recursive=True)
    assert not (upload / "data").exists()


def test_delete_inbox_entry_removes_a_file(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    upload = workspace / "upload"
    upload.mkdir(parents=True)
    (upload / "a.txt").write_text("a", encoding="utf-8")

    inbox.delete_inbox_entry(workspace, "upload/a.txt")

    assert not (upload / "a.txt").exists()


def test_batch_delete_dedupes_children_and_reports_freed_bytes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    upload = workspace / "upload"
    (upload / "dir").mkdir(parents=True)
    (upload / "dir" / "a.txt").write_bytes(b"a" * 4096)
    (upload / "b.txt").write_bytes(b"b" * 4096)

    result = inbox.delete_inbox_entries(
        workspace,
        ["upload/dir/a.txt", "upload/dir", "upload/b.txt"],
        recursive=True,
    )

    assert result["deleted"] == ["upload/dir", "upload/b.txt"]
    assert result["failed"] == []
    assert result["freed_bytes"] >= 8192
    assert not (upload / "dir").exists()
    assert not (upload / "b.txt").exists()


def test_batch_delete_validates_every_path_before_deleting(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    upload = workspace / "upload"
    upload.mkdir(parents=True)
    (upload / "a.txt").write_text("a", encoding="utf-8")
    (workspace / "secret.txt").write_text("secret", encoding="utf-8")

    with pytest.raises(inbox.InboxUploadError):
        inbox.delete_inbox_entries(
            workspace,
            ["upload/a.txt", "secret.txt"],
            recursive=True,
        )

    assert (upload / "a.txt").is_file()
    assert (workspace / "secret.txt").is_file()


def test_batch_delete_reports_a_path_that_is_already_gone(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    (workspace / "upload").mkdir(parents=True)

    result = inbox.delete_inbox_entries(
        workspace,
        ["upload/missing.txt"],
        recursive=True,
    )

    assert result["deleted"] == []
    assert result["failed"][0]["error"] == "文件不存在"


def test_describe_inbox_entries_reports_kind_and_size(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    upload = workspace / "upload"
    (upload / "dir").mkdir(parents=True)
    (upload / "dir" / "a.txt").write_bytes(b"a" * 2048)

    rows = inbox.describe_inbox_entries(workspace, ["upload/dir"])

    assert rows[0]["path"] == "upload/dir"
    assert rows[0]["kind"] == "directory"
    assert rows[0]["bytes"] >= 2048
    assert rows[0]["exists"] is True


def test_expand_upload_patterns_stays_inside_upload(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    upload = workspace / "upload"
    (upload / "logs" / "nested").mkdir(parents=True)
    (upload / "logs" / "a.log").write_text("a", encoding="utf-8")
    (upload / "logs" / "nested" / "c.log").write_text("c", encoding="utf-8")
    (upload / "b.txt").write_text("b", encoding="utf-8")

    assert inbox.expand_upload_patterns(workspace, ["*.txt"]) == [
        "upload/b.txt",
    ]
    assert set(inbox.expand_upload_patterns(workspace, ["**/*.log"])) == {
        "upload/logs/a.log",
        "upload/logs/nested/c.log",
    }


@pytest.mark.parametrize(
    "pattern",
    ["", "..", "../*", "/etc/*", "upload/.jobs/*", "upload"],
)
def test_expand_upload_patterns_rejects_unsafe_rules(
    tmp_path: Path,
    pattern: str,
) -> None:
    workspace = tmp_path / "ws"
    (workspace / "upload").mkdir(parents=True)

    with pytest.raises(inbox.InboxUploadError):
        inbox.expand_upload_patterns(workspace, [pattern])


def test_expand_upload_patterns_respects_the_limit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "ws"
    upload = workspace / "upload"
    upload.mkdir(parents=True)
    for index in range(3):
        (upload / f"{index}.txt").write_text("x", encoding="utf-8")

    with pytest.raises(inbox.InboxUploadError, match="匹配项超过"):
        inbox.expand_upload_patterns(workspace, ["*.txt"], limit=2)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "upload",
        "upload/",
        "../secret.txt",
        "upload/../secret.txt",
        "project/a.txt",
        "/etc/passwd",
        "upload/.jobs/x.json",
        "upload/\x00",
    ],
)
def test_inbox_entry_path_refuses_anything_outside_upload(
    tmp_path: Path,
    path: str,
) -> None:
    workspace = tmp_path / "ws"
    (workspace / "upload" / ".jobs").mkdir(parents=True)
    (workspace / "secret.txt").write_text("secret", encoding="utf-8")

    with pytest.raises(inbox.InboxUploadError):
        inbox.inbox_entry_path(workspace, path)


def test_bad_archive_fails_but_keeps_the_archive(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    archive = workspace / "upload" / "bad.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"not a zip")

    job = inbox.create_extract_job(workspace, archive)
    inbox.run_extract_job(workspace, job["id"])

    stored = inbox.read_job(workspace, job["id"])
    assert stored is not None
    assert stored["status"] == "failed"
    assert stored["error"]
    assert archive.is_file(), "the original upload must survive a failed extraction"
    assert not Path(stored["target"]).exists()


def test_invalid_job_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(inbox.InboxUploadError):
        inbox.read_job(tmp_path, "../../etc/passwd")


@pytest.mark.parametrize(
    "name",
    ["../x", "a/../../b", "/etc/passwd", "C:/Windows/x"],
)
def test_member_parts_rejects_unsafe_names(name: str) -> None:
    """条目的显式校验：即使下游还有 _within 兜底，这里也必须拒绝。"""
    with pytest.raises(inbox.ArchiveError):
        inbox._member_parts(name)


def test_extract_zip_rejects_a_bomb_hidden_among_normal_files(
    tmp_path: Path,
) -> None:
    """整体压缩比正常、单个条目是炸弹时也要拒绝。

    20MB 随机数据让整包压缩比接近 1，总量检查不会触发；只有单条目压缩比
    （4MB 的零 → 几 KB）能识别出来。
    """
    import os as _os

    archive = _zip(
        tmp_path / "mixed.zip",
        {
            "noise.bin": _os.urandom(20 * 1024 * 1024),
            "big.bin": b"\x00" * (4 * 1024 * 1024),
        },
    )
    with pytest.raises(inbox.ArchiveError, match="压缩比"):
        inbox.extract_archive(archive, tmp_path / "out")
