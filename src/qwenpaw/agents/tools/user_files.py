"""Resolve the files a chat user may hand to a write tool.

Tools that persist user data (data-table import, knowledge upload) must only
ever touch files inside the calling account's own workspace. The model supplies
a loose reference — usually the attachment filename it just saw, sometimes a
path — so resolution happens here, together with the containment checks.

Approval is hard-disabled in this deployment (the governance policy engine is
never consulted), so these checks are the actual gate, not the tool policy
declarations.
"""

from __future__ import annotations

import glob
import os
import re
from pathlib import Path

from ...app.agent_context import get_current_agent_id, get_current_user_id
from ...config.context import get_current_workspace_dir

# How many entries the "did you mean" hint lists when nothing matched.
_MAX_HINTS = 10

# Chat uploads are stored as ``<32 hex>_<original name>`` (see
# ``app/routers/console.py``); the prefix is noise in user-facing names.
_UPLOAD_PREFIX_RE = re.compile(r"^[0-9a-f]{32}_")


def user_facing_name(path: Path) -> str:
    """Return the original file name for a stored chat upload."""
    return _UPLOAD_PREFIX_RE.sub("", path.name) or path.name


def resolve_account_username() -> str:
    """Resolve the owning account for both interactive and scheduled runs."""
    username = get_current_user_id()
    if username and username != "default":
        return username
    try:
        from ...config import load_config

        profile = load_config().agents.profiles.get(get_current_agent_id())
        owner = getattr(profile, "owner_username", None) if profile else None
        if isinstance(owner, str) and owner.strip():
            return owner.strip()
    except Exception:
        pass
    return "admin"


def media_dir_for_current_agent() -> Path | None:
    """Return the console channel's attachment directory for this agent.

    Mirrors ``ConsoleChannel.media_dir``: an explicit ``CONSOLE_MEDIA_DIR``
    wins, otherwise chat uploads land in ``<workspace>/media``.
    """
    override = os.environ.get("CONSOLE_MEDIA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    workspace = get_current_workspace_dir()
    return Path(workspace).expanduser() / "media" if workspace else None


def allowed_roots(username: str) -> list[Path]:
    """Return the directories this account's tools may read uploads from."""
    candidates: list[Path] = []
    media = media_dir_for_current_agent()
    if media is not None:
        candidates.append(media)
    workspace = get_current_workspace_dir()
    if workspace:
        candidates.append(Path(workspace).expanduser())
    try:
        from ...app.user_space import get_user_root

        candidates.append(get_user_root(username))
    except Exception:
        pass

    roots: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _check_path(path: Path, roots: list[Path], allowed_exts: frozenset[str]) -> str | None:
    """Return an error reason for *path*, or ``None`` when it is usable.

    ``resolve()`` is what makes a symlink harmless: it dereferences the link
    first, so a link pointing outside the account workspace fails the
    containment check below instead of escaping it. Checking the unresolved
    path for symlinked components would instead reject deployments whose
    workspace root is itself a symlink.
    """
    try:
        resolved = path.resolve()
    except OSError as exc:
        return f"无法解析文件路径：{exc}"
    if not any(_inside(resolved, root) for root in roots):
        return "该文件不在你的工作区内，只能处理你自己上传或创建的文件。"
    if not resolved.is_file():
        return "文件不存在。"
    suffix = resolved.suffix.lower()
    if suffix not in allowed_exts:
        allowed = "、".join(sorted(allowed_exts))
        return f"不支持的文件格式：{suffix or '无扩展名'}。支持的格式：{allowed}。"
    return None


def _hints(roots: list[Path], allowed_exts: frozenset[str]) -> str:
    """List recently modified candidate files so the model can self-correct."""
    found: list[tuple[float, Path]] = []
    for root in roots:
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_file() and entry.suffix.lower() in allowed_exts:
                    found.append((entry.stat().st_mtime, entry))
            except OSError:
                continue
    if not found:
        return ""
    found.sort(key=lambda item: item[0], reverse=True)
    names = "、".join(entry.name for _, entry in found[:_MAX_HINTS])
    return f"当前可用的文件有：{names}"


def _search_by_name(name: str, roots: list[Path]) -> Path | None:
    """Find the newest file whose stored name ends with *name*, media dir first."""
    matches: list[tuple[float, Path]] = []
    for root in roots:
        for candidate in (root / name, *root.glob(f"*{glob.escape(name)}")):
            try:
                if candidate.is_file():
                    matches.append((candidate.stat().st_mtime, candidate))
            except OSError:
                continue
        if matches:
            break
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1]


def resolve_user_file(
    ref: str,
    allowed_exts: frozenset[str],
) -> tuple[Path | None, str]:
    """Resolve a model-supplied reference to a readable path inside the account.

    Returns ``(path, "")`` on success or ``(None, reason)`` with a message the
    model can act on (including a hint listing what is actually available).
    """
    ref = (ref or "").strip()
    if not ref:
        return None, "请提供要处理的文件名，例如 销售数据.xlsx。"

    username = resolve_account_username()
    roots = allowed_roots(username)
    if not roots:
        return None, "无法确定你的工作区目录，请联系管理员。"

    try:
        from ...app.channels.utils import file_url_to_local_path

        local = file_url_to_local_path(ref)
    except Exception:
        local = None
    candidate = Path(local or ref).expanduser()

    if candidate.is_absolute():
        error = _check_path(candidate, roots, allowed_exts)
        if error:
            return None, error
        return candidate.resolve(), ""

    # A relative reference may point into a subdirectory; a bare filename is
    # matched against the end of the stored name, because chat uploads are
    # saved as ``<uuid>_<original name>``.
    if candidate.parent != Path("."):
        for root in roots:
            error = _check_path(root / candidate, roots, allowed_exts)
            if error is None:
                return (root / candidate).resolve(), ""
        return None, f"没有找到 {candidate.as_posix()}，只能处理你自己工作区内的文件。"

    match = _search_by_name(candidate.name, roots)
    if match is None:
        hint = _hints(roots, allowed_exts)
        return None, f"没有找到名为 {candidate.name} 的文件。" + (f"\n{hint}" if hint else "")

    error = _check_path(match, roots, allowed_exts)
    if error:
        return None, error
    return match.resolve(), ""
