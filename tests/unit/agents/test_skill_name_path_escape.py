# -*- coding: utf-8 -*-
"""技能名不得成为路径注入点（#8065）。

背景：技能名可以来自 zip 里 ``SKILL.md`` 的 frontmatter，属于外部输入。
修复前 ``import_skill_dir`` 直接用 ``target_root / skill_name`` 拼路径，
``../x`` 或 ``/abs/x`` 会写到技能根目录之外（已实测）。``staged_skill_dir``
同样把名字拼进临时目录，虽然当前调用点都先做了 normalize，但函数本身不应
依赖调用方的自觉。

这组测试先在修复前跑应失败（``test_zip_import_cannot_escape_root``），
修复后全部通过；把 ``safe_skill_dir`` 回退成直接拼接，前三条必须重新失败。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from qwenpaw.agents.skill_system.store import (
    SkillsError,
    import_skill_dir,
    staged_skill_dir,
)


def _skill_zip(name: str) -> bytes:
    """Build a skill zip whose frontmatter declares *name*."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "skill/SKILL.md",
            f"---\nname: {name}\ndescription: proof\n---\n\nbody\n",
        )
        zf.writestr("skill/PROOF.txt", "escaped")
    return buf.getvalue()


def _extract(tmp_path: Path, name: str) -> tuple[Path, str]:
    """Extract *name*'s zip and return (source dir, declared name) as the API does."""
    from qwenpaw.agents.skill_system.store import extract_zip_skills

    tmp_dir, found = extract_zip_skills(_skill_zip(name))
    assert found, "zip should contain one skill"
    src_dir, resolved_name = found[0]
    return src_dir, resolved_name


@pytest.mark.parametrize(
    "declared",
    ["../ESCAPE", "../../ESCAPE", "/tmp/ESCAPE", "a/b"],
)
def test_zip_import_cannot_escape_root(tmp_path: Path, declared: str) -> None:
    """zip 里声明的名字不能把文件写到技能根目录之外。"""
    skills_root = tmp_path / "skills"
    skills_root.mkdir()

    src_dir, name = _extract(tmp_path, declared)

    with pytest.raises(SkillsError):
        import_skill_dir(src_dir, skills_root, name)

    # 根目录外不能出现任何被写入的文件
    assert not (tmp_path / "ESCAPE").exists()
    assert not (tmp_path.parent / "ESCAPE").exists()
    assert list(skills_root.iterdir()) == []


def test_zip_import_still_accepts_a_normal_name(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    skills_root.mkdir()

    src_dir, name = _extract(tmp_path, "中文 技能-01")
    assert import_skill_dir(src_dir, skills_root, name) is True

    installed = skills_root / name
    assert (installed / "SKILL.md").is_file()
    assert (installed / "PROOF.txt").is_file()


def test_staged_skill_dir_rejects_traversal() -> None:
    """函数自带的保证：即使调用方没 normalize，也不能逃出临时根目录。"""
    with pytest.raises(SkillsError):
        with staged_skill_dir("../escape"):
            pass


def test_staged_skill_dir_uses_a_fixed_prefix() -> None:
    """临时目录名里不应出现技能名（避免名字影响路径与日志）。"""
    with staged_skill_dir("我的技能") as staged:
        assert "我的技能" not in staged.parent.name
        assert staged.name == "我的技能"
