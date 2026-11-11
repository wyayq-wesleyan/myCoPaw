# -*- coding: utf-8 -*-
"""技能导入必须保留二进制文件（上游 #8198）。

GitHub/hub 技能包里除了 ``SKILL.md`` 和脚本，还可能有图片、字体、模板等二进制
附件。旧实现把内容一律按 ``str`` 读取并 ``write_text`` 写盘，二进制会损坏或被
直接跳过。修复后：``files`` 映射允许 ``bytes``，``SKILL.md`` 按 UTF-8 解码，
其余文件原样 ``write_bytes``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qwenpaw.agents.skill_system.hub import (
    _files_to_tree,
    _normalize_bundle,
)
from qwenpaw.agents.skill_system.store import (
    SkillsError,
    _create_files_from_tree,
)


def _skill_md(name: str = "demo") -> str:
    return f"---\nname: {name}\ndescription: demo\n---\n\nbody\n"


def test_normalize_bundle_accepts_bytes_skill_md_and_binary_assets() -> None:
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
    name, content, references, scripts, extra = _normalize_bundle(
        {
            "name": "demo",
            "files": {
                "SKILL.md": _skill_md().encode("utf-8"),
                "assets/logo.png": png,
                "notes.txt": "hello",
            },
        },
    )

    assert name == "demo"
    assert "body" in content
    assert extra["assets"]["logo.png"] == png
    assert extra["notes.txt"] == "hello"


def test_normalize_bundle_rejects_non_utf8_skill_md() -> None:
    with pytest.raises(SkillsError):
        _normalize_bundle(
            {
                "name": "demo",
                "files": {"SKILL.md": b"\xff\xfe\x00\x00"},
            },
        )


def test_files_to_tree_keeps_bytes() -> None:
    png = b"\x89PNG\r\n\x1a\n"
    references, scripts = _files_to_tree(
        {
            "references/a.png": png,
            "scripts/run.sh": "echo hi\n",
        },
    )

    assert references["a.png"] == png
    assert scripts["run.sh"] == "echo hi\n"


def test_create_files_from_tree_writes_binary_verbatim(tmp_path: Path) -> None:
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    _create_files_from_tree(
        tmp_path,
        {"assets": {"logo.png": png}, "notes.txt": "hello"},
    )

    assert (tmp_path / "assets" / "logo.png").read_bytes() == png
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "hello"
