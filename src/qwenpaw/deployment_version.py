# -*- coding: utf-8 -*-
"""Read the deployment build stamp shipped with a code-update package.

The semantic ``__version__`` changes only when the upstream project releases;
operators need a unique identifier for every code package they build, because
several servers may be updated at different times. ``app/VERSION`` is generated
by the packaging scripts and mounted with the application code.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from .__version__ import __version__


def _candidate_paths() -> list[Path]:
    candidates: list[Path] = []
    code_dir = os.environ.get("QWENPAW_CODE_DIR", "").strip()
    if code_dir:
        candidates.append(Path(code_dir) / "VERSION")
    # src/qwenpaw/deployment_version.py -> src/qwenpaw -> src -> app root
    candidates.append(Path(__file__).resolve().parents[2] / "VERSION")
    candidates.append(Path(__file__).resolve().parents[1] / "VERSION")
    return candidates


def _parse(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


@lru_cache(maxsize=1)
def deployment_version_info() -> dict[str, str]:
    """Return the build stamp, falling back to the semantic version."""
    values: dict[str, str] = {}
    for path in _candidate_paths():
        values = _parse(path)
        if values:
            break

    return {
        "deployment_version": values.get("version") or __version__,
        "semantic_version": values.get("semantic_version") or __version__,
        "build": values.get("build") or "unknown",
        "commit": values.get("commit") or "unknown",
        "branch": values.get("branch") or "unknown",
        "package": values.get("package") or "",
        "dirty": values.get("dirty") or "0",
        "knowledge_version": values.get("knowledge_version") or "",
    }


__all__ = ["deployment_version_info"]
