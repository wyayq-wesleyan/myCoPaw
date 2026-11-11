# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from qwenpaw.deployment_version import deployment_version_info


def test_reads_the_package_build_stamp(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text(
        "version=20261110.165900\n"
        "semantic_version=2.1.0.post4\n"
        "build=2026-11-10T16:59:00+08:00\n"
        "commit=abc1234\n"
        "branch=feature/test\n"
        "package=huizhi-code-update-20261110.tar.gz\n"
        "dirty=1\n"
        "knowledge_version=0.1.4\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("QWENPAW_CODE_DIR", str(tmp_path))
    deployment_version_info.cache_clear()
    try:
        info = deployment_version_info()
    finally:
        deployment_version_info.cache_clear()

    assert info["deployment_version"] == "20261110.165900"
    assert info["semantic_version"] == "2.1.0.post4"
    assert info["commit"] == "abc1234"
    assert info["package"] == "huizhi-code-update-20261110.tar.gz"
    assert info["knowledge_version"] == "0.1.4"


def test_falls_back_to_semantic_version(tmp_path, monkeypatch):
    monkeypatch.setenv("QWENPAW_CODE_DIR", str(tmp_path))
    deployment_version_info.cache_clear()
    try:
        info = deployment_version_info()
    finally:
        deployment_version_info.cache_clear()

    assert info["deployment_version"]
    assert info["build"] == "unknown"
