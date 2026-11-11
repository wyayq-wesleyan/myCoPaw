# -*- coding: utf-8 -*-
"""Per-account Linux execution sandbox for ordinary users.

This module is the containment boundary for ``execute_shell_command``.  It is
deliberately separate from the generic ``qwenpaw.sandbox`` backends: those are
feature-gated, report unenforced constraints, and can fall back to direct
execution.  Ordinary-user shell execution must not have such a fallback.

Design
------
* **Landlock allow-list** (Linux 5.13+): the child process may read only
  system runtime directories plus its own account root, and may write only its
  own ``outputs/`` directory.  App source (``/workspace/app``), ``working.secret``,
  other accounts, ``/data`` and ``/proc`` are outside the allow-list and are
  denied by the kernel.  The allow-list cannot be widened from inside the
  sandbox and survives ``exec``.
* **seccomp** (libseccomp): blocks non-AF_UNIX sockets, io_uring, bpf, ptrace,
  mount/unshare/chroot, device-node creation and handle-based opens.  Landlock
  already blocks TCP connect; seccomp also blocks UDP/raw sockets and other
  escape primitives.
* **Per-account UID/GID**: the child drops to a deterministic, otherwise
  unused numeric UID.  Its own ``outputs/`` and ``scripts/`` are writable; ``media/`` and
  ``upload/`` are made readable by that UID once.  A per-account UID also makes
  ``kill`` and PID probes harmless across accounts.
* **Resource limits**: address space, CPU time, file size, open files and
  process count are capped with ``setrlimit``.  The container still owns the
  outer cgroup; these limits stop one command from consuming the whole
  container.

If any of these cannot be installed the command is **refused**.  There is no
"fall back to host execution" path for an ordinary account.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import signal
import stat as stat_module
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from .audit import record_sandbox_execution
from .config import ExecutionResult

logger = logging.getLogger(__name__)

_LANDLOCK_SYS_CREATE = 444
_LANDLOCK_SYS_ADD_RULE = 445
_LANDLOCK_SYS_RESTRICT = 446
_LANDLOCK_RULE_PATH_BENEATH = 1

_FS_READ = (1 << 2) | (1 << 3)          # READ_FILE | READ_DIR
_FS_WRITE = (
    (1 << 1)                            # WRITE_FILE
    | (1 << 4) | (1 << 5)               # REMOVE_DIR | REMOVE_FILE
    | (1 << 6) | (1 << 7) | (1 << 8)    # MAKE_*
    | (1 << 9) | (1 << 10) | (1 << 11) | (1 << 12)
)
_FS_EXEC = 1 << 0
_FS_REFER = 1 << 13
_FS_TRUNCATE = 1 << 14
_FS_ALL = _FS_READ | _FS_WRITE | _FS_EXEC | _FS_REFER | _FS_TRUNCATE

_NET_BIND_TCP = 1 << 0
_NET_CONNECT_TCP = 1 << 1

_SYSTEM_READ_DIRS = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/dev")

# Environment variables that must never reach user code.  The allow-list
# builder starts from an empty dict, so this is defence in depth against a
# future caller passing os.environ by mistake.
_SECRET_MARKERS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "DATABASE_URL",
    "DSN",
    "JWT",
    "PRIVATE",
)

_DEFAULT_MEMORY_MB = 16384
_DEFAULT_FILE_MB = 4096
_DEFAULT_NOFILE = 1024
_DEFAULT_NPROC = 512


def _landlock_abi() -> int:
    """Return the Landlock ABI, or 0 when the kernel does not provide it."""
    if sys.platform != "linux":
        return 0
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        libc.syscall.restype = ctypes.c_long
        abi = libc.syscall(
            ctypes.c_long(_LANDLOCK_SYS_CREATE),
            None,
            ctypes.c_size_t(0),
            ctypes.c_uint32(1),
        )
        return int(abi) if abi >= 1 else 0
    except (OSError, AttributeError):
        return 0


def _seccomp_available() -> bool:
    """Whether libseccomp can be loaded in this container."""
    if sys.platform != "linux":
        return False
    try:
        import ctypes

        ctypes.CDLL("libseccomp.so.2")
        return True
    except OSError:
        return False


def _landlock_enabled() -> bool:
    """Whether the deployment opted into the Landlock sandbox.

    Default is **off**: openEuler 5.10/24.03 kernels compiled Landlock out, so
    the product defaults to the UID/group permission sandbox. Set
    ``HUIZHI_SANDBOX_LANDLOCK=1`` only on a kernel that provides Landlock;
    in that mode a missing Landlock is an error rather than a silent fallback.
    """
    value = os.environ.get("HUIZHI_SANDBOX_LANDLOCK", "0").strip().lower()
    return value in ("1", "true", "yes", "on")


class UserSandboxError(RuntimeError):
    """The sandbox cannot be installed; the command must not run."""


@dataclass(frozen=True)
class SandboxIdentity:
    username: str
    uid: int
    gid: int


@dataclass(frozen=True)
class SandboxPaths:
    account_root: Path
    output_dir: Path  # the ``outputs/`` directory (name kept for callers)
    scripts_dir: Path
    media_dirs: tuple[Path, ...]
    upload_dir: Optional[Path]
    identity: SandboxIdentity
    permission_fallback: bool = False

    @property
    def outputs_dir(self) -> Path:
        """User-facing name for :attr:`output_dir`."""
        return self.output_dir


def _sandbox_uid(username: str) -> int:
    """Deterministic numeric UID in a range unlikely to collide with real users.

    20000-99999 is a normal system-UID range, so a future account created by
    the host cannot silently share the sandbox UID for the same name hash.
    The hash makes the value stable across container restarts, which matters:
    files in ``outputs/`` and ``scripts/`` keep their owner.
    """
    digest = hashlib.sha256(username.encode("utf-8")).digest()
    return 20000 + (int.from_bytes(digest[:8], "big") % 80000)


def identity_for(username: str) -> SandboxIdentity:
    uid = _sandbox_uid(username)
    return SandboxIdentity(username=username, uid=uid, gid=uid)


def _is_inside(path: Path, root: Path) -> bool:
    try:
        return path == root or path.is_relative_to(root)
    except ValueError:
        return False


def _media_dirs(account_root: Path) -> tuple[Path, ...]:
    """Media directories inside this account's root.

    Default agent media is ``<root>/media``; additional agents use
    ``<root>/agents/<id>/media``.  Only directories physically under the
    account root are returned, so a crafted symlink cannot add a mount.
    """
    found: list[Path] = []
    candidate = account_root / "media"
    if candidate.is_dir() and not candidate.is_symlink():
        found.append(candidate)
    agents = account_root / "agents"
    if agents.is_dir() and not agents.is_symlink():
        try:
            for entry in agents.iterdir():
                media = entry / "media"
                if media.is_dir() and not media.is_symlink():
                    found.append(media)
        except OSError:
            pass
    return tuple(found)


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    """Best-effort recursive ownership change without following symlinks."""
    if not path.exists() or path.is_symlink():
        return
    def _grant_other_read(target: Path) -> None:
        """Fallback when chown is not permitted (e.g. root-squashed NFS)."""
        try:
            if target.is_symlink():
                return
            mode = target.stat().st_mode
            extra = 0o005 if stat_module.S_ISDIR(mode) else 0o004
            os.chmod(target, stat_module.S_IMODE(mode) | extra)
        except OSError:
            pass

    for root, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            target = Path(root) / name
            try:
                if target.is_symlink():
                    continue
                os.chown(target, uid, gid)
            except OSError:
                _grant_other_read(target)
        try:
            os.chown(root, uid, gid)
        except OSError:
            _grant_other_read(Path(root))


def _prepare_writable_dir(path: Path, identity: SandboxIdentity) -> None:
    """Create *path* and make it writable by the sandbox UID."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chown(path, identity.uid, identity.gid)
            os.chmod(path, 0o770)
        except OSError:
            # chown unavailable: fall back to world-writable. Landlock still
            # confines every sandbox to these two paths.
            os.chmod(path, 0o777)
    except OSError:
        logger.warning("sandbox: could not prepare writable dir %s", path, exc_info=True)


def _prepare_permission_owner(paths: SandboxPaths) -> None:
    """Prepare the UID/group permission sandbox used when Landlock is absent.

    Layout::

        account root       root:<gid> 2750  (group traverse/read, no write)
        outputs/, scripts/ root:<gid> 2770  (group write)
        media/, upload/    root:<gid> 2750  (read-only for the sandbox)
        other files        root:<gid> 0640  (read-only)
        chats.json etc.    root:root  0600  (hidden from the sandbox)

    The account's sandbox UID gets the same numeric GID, so it can read its own
    tree and write only the two output directories. Other accounts have a
    different GID, so they cannot even traverse this account's root. Because
    the directories are root-owned, the sandbox cannot chmod its way back to
    write access.
    """
    root = paths.account_root
    gid = paths.identity.gid
    marker = root / ".sandbox-owner-perm"
    wanted = f"v3perm:{paths.identity.uid}:{gid}"
    already = False
    try:
        already = marker.is_file() and marker.read_text("utf-8").strip() == wanted
    except OSError:
        pass

    try:
        os.chmod(root.parent, 0o711)
    except OSError:
        pass

    if not already:
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            current = Path(dirpath)
            try:
                relative = current.relative_to(root)
            except ValueError:
                continue
            writable = bool(relative.parts) and relative.parts[0] in (
                "outputs",
                "scripts",
            )
            dir_mode = 0o2770 if writable else 0o2750
            file_mode = 0o660 if writable else 0o640
            for name in list(dirnames):
                target = current / name
                if target.is_symlink():
                    dirnames.remove(name)
                    continue
                try:
                    os.chown(target, 0, gid)
                    os.chmod(target, dir_mode)
                except OSError:
                    logger.debug("permission sandbox: could not prepare %s", target, exc_info=True)
            for name in filenames:
                target = current / name
                if target.is_symlink():
                    continue
                try:
                    os.chown(target, 0, gid)
                    os.chmod(target, file_mode)
                except OSError:
                    logger.debug("permission sandbox: could not prepare %s", target, exc_info=True)
        try:
            os.chown(root, 0, gid)
            os.chmod(root, 0o2750)
            for hidden in ("chats.json", "credentials.yaml", ".sandbox-owner"):
                target = root / hidden
                if target.is_file() and not target.is_symlink():
                    os.chown(target, 0, 0)
                    os.chmod(target, 0o600)
            marker.write_text(wanted + "\n", "utf-8")
            os.chown(marker, 0, 0)
            os.chmod(marker, 0o600)
        except OSError:
            logger.warning("permission sandbox: could not finalize %s", root, exc_info=True)
    else:
        try:
            os.chmod(root, 0o2750)
        except OSError:
            pass

    for directory in (paths.output_dir, paths.scripts_dir):
        if directory.is_symlink():
            directory.unlink()
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chown(directory, 0, gid)
            os.chmod(directory, 0o2770)
        except OSError:
            logger.warning("permission sandbox: could not prepare %s", directory, exc_info=True)
        for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
            for name in dirnames:
                target = Path(dirpath) / name
                if target.is_symlink():
                    continue
                try:
                    os.chown(target, 0, gid)
                    os.chmod(target, 0o2770)
                except OSError:
                    pass
            for name in filenames:
                target = Path(dirpath) / name
                if target.is_symlink():
                    continue
                try:
                    os.chown(target, 0, gid)
                    os.chmod(target, 0o660)
                except OSError:
                    pass


def _prepare_owner(paths: SandboxPaths) -> None:
    """Make outputs/scripts writable and media/upload readable by the sandbox.

    The app runs as root, so changing ownership does not affect the app.  The
    marker keeps this from walking a multi-gigabyte tree on every shell call.
    Existing files are made world-readable by the app's normal umask; the
    marker covers legacy files that were created 0600 by an older release.

    The two writable directories are re-checked on every call: they are cheap
    to prepare and may be created after the one-time tree walk (for example
    when an older account first runs after this version is installed).
    """
    if paths.permission_fallback:
        _prepare_permission_owner(paths)
        return

    root = paths.account_root
    marker = root / ".sandbox-owner"
    wanted = f"v2:{paths.identity.uid}:{paths.identity.gid}"
    already_owned = False
    try:
        already_owned = (
            marker.is_file() and marker.read_text("utf-8").strip() == wanted
        )
    except OSError:
        pass

    if not already_owned:
        # The whole account tree, not just media/upload/outputs/scripts: older
        # files may be 0600 root-owned (chats, skills, per-agent workspaces).
        # Landlock still makes the tree read-only for the sandbox; ownership
        # only makes the read possible.
        _chown_tree(paths.account_root, paths.identity.uid, paths.identity.gid)

    _prepare_writable_dir(paths.output_dir, paths.identity)
    _prepare_writable_dir(paths.scripts_dir, paths.identity)

    if not already_owned:
        try:
            marker.write_text(wanted + "\n", "utf-8")
        except OSError:
            logger.debug("sandbox: could not write ownership marker", exc_info=True)


def prepare_paths(
    username: str,
    permission_fallback: bool = False,
) -> SandboxPaths:
    """Resolve and prepare the caller's sandbox-visible directories.

    The writable layout is::

        outputs/   final files produced for the user
        scripts/   intermediate code written to produce those files
        output/    legacy directory, kept readable but no longer writable
    """
    from ..app.user_space import get_user_root

    account_root = get_user_root(username).expanduser().resolve()
    account_root.mkdir(parents=True, exist_ok=True)
    output_dir = account_root / "outputs"
    scripts_dir = account_root / "scripts"
    # A pre-existing symlink could point a writable directory (or a child) at
    # a secret and turn the writable Landlock rule into a grant on that target.
    # Replace symlinks with real directories; never follow them.
    for directory in (output_dir, scripts_dir):
        if directory.is_symlink():
            directory.unlink()
        directory.mkdir(parents=True, exist_ok=True)
    for sub in ("tmp", "home", ".cache", ".matplotlib"):
        child = output_dir / sub
        if child.is_symlink():
            child.unlink()
        child.mkdir(parents=True, exist_ok=True)

    media_dirs = _media_dirs(account_root)
    upload_dir = account_root / "upload"
    upload = upload_dir if upload_dir.is_dir() and not upload_dir.is_symlink() else None

    paths = SandboxPaths(
        account_root=account_root,
        output_dir=output_dir,
        scripts_dir=scripts_dir,
        media_dirs=media_dirs,
        upload_dir=upload,
        identity=identity_for(username),
        permission_fallback=permission_fallback,
    )
    _prepare_owner(paths)
    return paths


def prepare_all_accounts() -> int:
    """Harden every known account root for the permission fallback.

    Called once during an upgrade before the first shell of the new version:
    otherwise account B's root could still be world-readable when account A
    runs first. Also locks down orphaned roots so a deleted account's data is
    not reachable through a stale, permissive directory.
    """
    from ..app.auth import list_users
    from ..app.user_space import USER_WORKSPACE_ROOT, get_user_root

    # Logs, backups and the secret directory are siblings of the account
    # workspace. Their directories are root-only; files inside can stay 0644
    # because a sandbox UID cannot traverse the parent. Doing this on every
    # boot also covers files created after the previous hardening run.
    working = Path(os.environ.get("QWENPAW_WORKING_DIR", "/app/working"))
    private_working_dirs = (
        "users",
        "media",
        "memory",
        "plugins",
        "models",
        "local_models",
        "skill_pool",
        "chats",
        "sessions",
        "logs",
    )
    for path, mode in (
        (working, 0o711),
        (working / "workspaces", 0o711),
        *((working / name, 0o700) for name in private_working_dirs),
        (Path(os.environ.get("QWENPAW_SECRET_DIR", "/app/working.secret")), 0o700),
        (Path(os.environ.get("QWENPAW_BACKUP_DIR", "/app/working.backups")), 0o700),
    ):
        try:
            if path.exists() and not path.is_symlink():
                os.chmod(path, mode)
        except OSError:
            logger.debug("permission sandbox: could not chmod %s", path, exc_info=True)

    known: set[Path] = set()
    errors: list[str] = []
    try:
        users = list_users()
    except Exception as exc:  # noqa: BLE001 - orphan cleanup still runs
        logger.warning("permission sandbox: could not list users: %s", exc)
        users = []
    for item in users:
        username = str(item.get("username") or "").strip()
        if not username:
            continue
        try:
            prepare_paths(username, permission_fallback=True)
            known.add(get_user_root(username).resolve())
        except Exception as exc:  # noqa: BLE001 - report and continue
            errors.append(f"{username}: {exc}")

    root = Path(USER_WORKSPACE_ROOT)
    if root.is_dir():
        for child in root.iterdir():
            try:
                resolved = child.resolve()
            except OSError:
                continue
            if not child.is_dir() or child.is_symlink() or resolved in known:
                continue
            try:
                os.chown(child, 0, 0)
                os.chmod(child, 0o700)
            except OSError as exc:
                errors.append(f"orphan {child}: {exc}")

    if errors:
        raise RuntimeError("; ".join(errors))
    return len(known)


def _safe_value(name: str, value: str) -> bool:
    upper = name.upper()
    return not any(marker in upper for marker in _SECRET_MARKERS)


def build_safe_env(paths: SandboxPaths) -> dict[str, str]:
    """Environment for user code: no app secrets, no PYTHONPATH, no proxy vars."""
    env: dict[str, str] = {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(paths.output_dir / "home"),
        "TMPDIR": str(paths.output_dir / "tmp"),
        "XDG_CACHE_HOME": str(paths.output_dir / ".cache"),
        "MPLCONFIGDIR": str(paths.output_dir / ".matplotlib"),
        "PYTHONNOUSERSITE": "1",
        "HUIZHI_SANDBOX": "1",
        "HUIZHI_ACCOUNT_ROOT": str(paths.account_root),
        "HUIZHI_OUTPUT_DIR": str(paths.output_dir),
        "HUIZHI_SCRIPTS_DIR": str(paths.scripts_dir),
    }
    for name in ("LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM"):
        value = os.environ.get(name)
        if value and _safe_value(name, value):
            env[name] = value
    return env


def _resource_limits() -> tuple[int, int, int, int, int]:
    def _int_env(name: str, default: int) -> int:
        try:
            return max(1, int(os.environ.get(name, str(default))))
        except (TypeError, ValueError):
            return default

    return (
        _int_env("HUIZHI_SANDBOX_MEMORY_MB", _DEFAULT_MEMORY_MB),
        _int_env("HUIZHI_SANDBOX_FILE_MB", _DEFAULT_FILE_MB),
        _int_env("HUIZHI_SANDBOX_NOFILE", _DEFAULT_NOFILE),
        _int_env("HUIZHI_SANDBOX_NPROC", _DEFAULT_NPROC),
        _int_env("HUIZHI_SANDBOX_CPU_SECONDS", 0),
    )


def _generate_permission_runner_script(
    cmd: str,
    cwd: Path,
    paths: SandboxPaths,
    timeout: float,
) -> str:
    """Runner used when Landlock is unavailable.

    Filesystem isolation comes from the root-owned group modes prepared by
    :func:`_prepare_permission_owner`; this runner adds the parts Unix
    permissions cannot express: no new privileges, seccomp network/mount/
    ptrace blocks, a dedicated UID/GID, rlimits and a private umask.
    """
    memory_mb, file_mb, nofile, nproc, cpu_extra = _resource_limits()
    cpu_seconds = int(timeout) + max(5, cpu_extra) if timeout else 0
    uid = paths.identity.uid
    gid = paths.identity.gid
    account_root = str(paths.account_root)

    lines = [
        "import ctypes, errno, os, resource, sys",
        "",
        "libc = ctypes.CDLL(None, use_errno=True)",
        "libc.prctl.restype = ctypes.c_int",
        "libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]",
        "if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS",
        "    raise SystemExit('sandbox setup failed: no_new_privs')",
        "",
        "try:",
        "    seccomp = ctypes.CDLL('libseccomp.so.2')",
        "except OSError as exc:",
        "    raise SystemExit('sandbox setup failed: libseccomp unavailable: %s' % exc)",
        "SCMP_ACT_ALLOW = 0x7fff0000",
        "SCMP_ACT_ERRNO = 0x00050000 | errno.EPERM",
        "SCMP_CMP_NE = 1",
        "class Cmp(ctypes.Structure):",
        "    _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_int), ('datum_a', ctypes.c_uint64), ('datum_b', ctypes.c_uint64)]",
        "seccomp.seccomp_init.restype = ctypes.c_void_p",
        "seccomp.seccomp_init.argtypes = [ctypes.c_uint32]",
        "seccomp.seccomp_rule_add_array.restype = ctypes.c_int",
        "seccomp.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]",
        "seccomp.seccomp_load.restype = ctypes.c_int",
        "seccomp.seccomp_load.argtypes = [ctypes.c_void_p]",
        "seccomp.seccomp_release.argtypes = [ctypes.c_void_p]",
        "seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int",
        "seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]",
        "ctx = seccomp.seccomp_init(SCMP_ACT_ALLOW)",
        "if not ctx:",
        "    raise SystemExit('sandbox setup failed: seccomp_init')",
        "socket_nr = seccomp.seccomp_syscall_resolve_name(b'socket')",
        "if socket_nr < 0:",
        "    raise SystemExit('sandbox setup failed: socket syscall unresolved')",
        "cmp = Cmp(0, SCMP_CMP_NE, 1, 0)  # AF_UNIX = 1; everything else denied",
        "if seccomp.seccomp_rule_add_array(ctx, SCMP_ACT_ERRNO, socket_nr, 1, ctypes.byref(cmp)) != 0:",
        "    raise SystemExit('sandbox setup failed: seccomp socket rule')",
        "blocked = [b'io_uring_setup', b'io_uring_enter', b'io_uring_register',",
        "           b'bpf', b'perf_event_open', b'userfaultfd', b'keyctl',",
        "           b'ptrace', b'process_vm_readv', b'process_vm_writev',",
        "           b'mount', b'umount2', b'pivot_root', b'setns', b'unshare',",
        "           b'chroot', b'mknodat', b'mknod', b'open_by_handle_at',",
        "           b'mq_open', b'mq_unlink', b'mq_timedsend', b'mq_timedreceive',",
        "           b'mq_notify', b'mq_getsetattr']",
        "for name in blocked:",
        "    nr = seccomp.seccomp_syscall_resolve_name(name)",
        "    if nr >= 0 and seccomp.seccomp_rule_add_array(ctx, SCMP_ACT_ERRNO, nr, 0, None) != 0:",
        "        raise SystemExit('sandbox setup failed: seccomp rule %s' % name.decode())",
        "if seccomp.seccomp_load(ctx) != 0:",
        "    raise SystemExit('sandbox setup failed: seccomp_load')",
        "seccomp.seccomp_release(ctx)",
        "",
        f"uid = {uid}",
        f"gid = {gid}",
        "try:",
        "    os.setgroups([])",
        "    os.setgid(gid)",
        "    os.setuid(uid)",
        "except OSError as exc:",
        "    raise SystemExit('sandbox setup failed: drop privileges: %s' % exc)",
        "",
        "def limit(kind, soft, hard=None):",
        "    hard = soft if hard is None else hard",
        "    try:",
        "        resource.setrlimit(kind, (soft, hard))",
        "    except (ValueError, OSError):",
        "        pass",
        "",
        f"limit(resource.RLIMIT_AS, {memory_mb * 1024 * 1024})",
        f"limit(resource.RLIMIT_FSIZE, {file_mb * 1024 * 1024})",
        f"limit(resource.RLIMIT_NOFILE, {nofile})",
        f"limit(resource.RLIMIT_NPROC, {nproc})",
        "limit(resource.RLIMIT_CORE, 0)",
    ]
    if cpu_seconds:
        lines.append(f"limit(resource.RLIMIT_CPU, {cpu_seconds})")
    lines += [
        "os.umask(0o077)",
        f"cwd = {str(cwd)!r}",
        "try:",
        "    os.chdir(cwd)",
        "except OSError:",
        f"    os.chdir({account_root!r})",
        f"os.execv('/bin/sh', ['/bin/sh', '-c', {cmd!r}])",
        "",
    ]
    return "\n".join(lines)


def _generate_runner_script(
    cmd: str,
    cwd: Path,
    paths: SandboxPaths,
    timeout: float,
) -> str:
    if paths.permission_fallback:
        return _generate_permission_runner_script(cmd, cwd, paths, timeout)

    memory_mb, file_mb, nofile, nproc, cpu_extra = _resource_limits()
    cpu_seconds = int(timeout) + max(5, cpu_extra) if timeout else 0
    account_root = str(paths.account_root)
    output_dir = str(paths.output_dir)
    scripts_dir = str(paths.scripts_dir)
    uid = paths.identity.uid
    gid = paths.identity.gid

    # Keep this script dependency-free: it must run before any import from the
    # app package (which the sandbox is about to hide).
    lines = [
        "import ctypes, ctypes.util, errno, os, resource, struct, sys",
        "",
        "FS_READ = (1 << 2) | (1 << 3)",
        "FS_WRITE = ((1 << 1) | (1 << 4) | (1 << 5) | (1 << 6) | (1 << 7) |"
        " (1 << 8) | (1 << 9) | (1 << 10) | (1 << 11) | (1 << 12))",
        "FS_EXEC = 1 << 0",
        "NET_ALL = (1 << 0) | (1 << 1)",
        "",
        "libc = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6', use_errno=True)",
        "libc.syscall.restype = ctypes.c_long",
        "libc.prctl.restype = ctypes.c_int",
        "libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]",
        "if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS",
        "    raise SystemExit('sandbox setup failed: no_new_privs')",
        "",
        "abi = libc.syscall(444, None, ctypes.c_size_t(0), ctypes.c_uint32(1))",
        "FS_HANDLED = FS_READ | FS_WRITE | FS_EXEC",
        "if abi >= 2:",
        "    FS_HANDLED |= (1 << 13)",
        "if abi >= 3:",
        "    FS_HANDLED |= (1 << 14)",
        "HANDLED_NET = NET_ALL if abi >= 4 else 0",
        "if HANDLED_NET:",
        "    attr = struct.pack('QQ', FS_HANDLED, HANDLED_NET)",
        "else:",
        "    attr = struct.pack('Q', FS_HANDLED)",
        "abuf = ctypes.create_string_buffer(attr)",
        f"ruleset = libc.syscall({_LANDLOCK_SYS_CREATE}, ctypes.cast(abuf, ctypes.c_void_p), ctypes.c_size_t(len(attr)), ctypes.c_uint32(0))",
        "if ruleset < 0:",
        "    raise SystemExit('sandbox setup failed: landlock_create_ruleset errno=%d' % ctypes.get_errno())",
        "",
        "def add_path(path, access, critical=False):",
        "    try:",
        "        pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)",
        "    except OSError as exc:",
        "        if critical:",
        "            raise SystemExit('sandbox setup failed: open %s: %s' % (path, exc))",
        "        return",
        "    try:",
        "        raw = struct.pack('Qi', access, pfd)",
        "        buf = ctypes.create_string_buffer(raw)",
        f"        rc = libc.syscall({_LANDLOCK_SYS_ADD_RULE}, ctypes.c_int(ruleset), ctypes.c_int({_LANDLOCK_RULE_PATH_BENEATH}), ctypes.cast(buf, ctypes.c_void_p), ctypes.c_uint32(0))",
        "        if rc < 0 and critical:",
        "            raise SystemExit('sandbox setup failed: landlock_add_rule %s errno=%d' % (path, ctypes.get_errno()))",
        "    finally:",
        "        os.close(pfd)",
        "",
    ]
    for path in _SYSTEM_READ_DIRS:
        if os.path.exists(path):
            lines.append(f"add_path({path!r}, FS_READ | FS_EXEC)")
    # /dev is a directory and exposes device nodes; device-cgroup plus the
    # seccomp blocks below keep this narrow.
    if os.path.exists("/dev"):
        lines.append("add_path('/dev', FS_READ | FS_WRITE | FS_EXEC)")
    lines += [
        f"add_path({account_root!r}, FS_READ, critical=True)",
        f"add_path({output_dir!r}, FS_HANDLED, critical=True)",
        f"add_path({scripts_dir!r}, FS_HANDLED, critical=True)",
        "",
        f"rc = libc.syscall({_LANDLOCK_SYS_RESTRICT}, ctypes.c_int(ruleset), ctypes.c_uint32(0))",
        "os.close(ruleset)",
        "if rc != 0:",
        "    raise SystemExit('sandbox setup failed: landlock_restrict_self errno=%d' % ctypes.get_errno())",
        "",
    ]

    # seccomp: no inet/packet/netlink sockets, no namespace/mount/escape paths.
    lines += [
        "try:",
        "    seccomp = ctypes.CDLL('libseccomp.so.2')",
        "except OSError as exc:",
        "    raise SystemExit('sandbox setup failed: libseccomp unavailable: %s' % exc)",
        "SCMP_ACT_ALLOW = 0x7fff0000",
        "SCMP_ACT_ERRNO = 0x00050000 | errno.EPERM",
        "SCMP_CMP_NE = 1",
        "class Cmp(ctypes.Structure):",
        "    _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_int), ('datum_a', ctypes.c_uint64), ('datum_b', ctypes.c_uint64)]",
        "seccomp.seccomp_init.restype = ctypes.c_void_p",
        "seccomp.seccomp_init.argtypes = [ctypes.c_uint32]",
        "seccomp.seccomp_rule_add_array.restype = ctypes.c_int",
        "seccomp.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]",
        "seccomp.seccomp_load.restype = ctypes.c_int",
        "seccomp.seccomp_load.argtypes = [ctypes.c_void_p]",
        "seccomp.seccomp_release.argtypes = [ctypes.c_void_p]",
        "seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int",
        "seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]",
        "ctx = seccomp.seccomp_init(SCMP_ACT_ALLOW)",
        "if not ctx:",
        "    raise SystemExit('sandbox setup failed: seccomp_init')",
        "socket_nr = seccomp.seccomp_syscall_resolve_name(b'socket')",
        "if socket_nr < 0:",
        "    raise SystemExit('sandbox setup failed: socket syscall unresolved')",
        "cmp = Cmp(0, SCMP_CMP_NE, 1, 0)  # AF_UNIX = 1; everything else denied",
        "if seccomp.seccomp_rule_add_array(ctx, SCMP_ACT_ERRNO, socket_nr, 1, ctypes.byref(cmp)) != 0:",
        "    raise SystemExit('sandbox setup failed: seccomp socket rule')",
        "blocked = [b'io_uring_setup', b'io_uring_enter', b'io_uring_register',",
        "           b'bpf', b'perf_event_open', b'userfaultfd', b'keyctl',",
        "           b'ptrace', b'process_vm_readv', b'process_vm_writev',",
        "           b'mount', b'umount2', b'pivot_root', b'setns', b'unshare',",
        "           b'chroot', b'mknodat', b'open_by_handle_at',",
        "           b'mq_open', b'mq_unlink', b'mq_timedsend', b'mq_timedreceive',",
        "           b'mq_notify', b'mq_getsetattr']",
        "if abi < 3:",
        "    # ABI 1/2 do not mediate truncate; block it outright on old kernels.",
        "    blocked += [b'truncate', b'ftruncate']",
        "if abi < 2:",
        "    # ABI 1 has no REFER: a hardlink/rename could alias a read-only file",
        "    # into outputs/ or scripts/.  Block those operations on the oldest kernels.",
        "    blocked += [b'link', b'linkat', b'rename', b'renameat', b'renameat2']",
        "for name in blocked:",
        "    nr = seccomp.seccomp_syscall_resolve_name(name)",
        "    if nr >= 0 and seccomp.seccomp_rule_add_array(ctx, SCMP_ACT_ERRNO, nr, 0, None) != 0:",
        "        raise SystemExit('sandbox setup failed: seccomp rule %s' % name.decode())",
        "if seccomp.seccomp_load(ctx) != 0:",
        "    raise SystemExit('sandbox setup failed: seccomp_load')",
        "seccomp.seccomp_release(ctx)",
        "",
    ]

    # Drop to the per-account UID after Landlock/seccomp are installed.
    lines += [
        f"uid = {uid}",
        f"gid = {gid}",
        "try:",
        "    os.setgroups([])",
        "    os.setgid(gid)",
        "    os.setuid(uid)",
        "except OSError as exc:",
        "    raise SystemExit('sandbox setup failed: drop privileges: %s' % exc)",
        "",
        "def limit(kind, soft, hard=None):",
        "    hard = soft if hard is None else hard",
        "    try:",
        "        resource.setrlimit(kind, (soft, hard))",
        "    except (ValueError, OSError):",
        "        pass",
        "",
        f"limit(resource.RLIMIT_AS, {memory_mb * 1024 * 1024})",
        f"limit(resource.RLIMIT_FSIZE, {file_mb * 1024 * 1024})",
        f"limit(resource.RLIMIT_NOFILE, {nofile})",
        f"limit(resource.RLIMIT_NPROC, {nproc})",
        "limit(resource.RLIMIT_CORE, 0)",
    ]
    if cpu_seconds:
        lines.append(f"limit(resource.RLIMIT_CPU, {cpu_seconds})")
    lines += [
        "os.umask(0o022)",
        f"cwd = {str(cwd)!r}",
        "try:",
        "    os.chdir(cwd)",
        "except OSError:",
        f"    os.chdir({account_root!r})",
        f"os.execv('/bin/sh', ['/bin/sh', '-c', {cmd!r}])",
        "",
    ]
    return "\n".join(lines)


def _supported() -> bool:
    """Whether a safe runner can be installed.

    Landlock is preferred. When the kernel was built without it, the explicit
    UID/permission fallback still requires libseccomp: without socket filtering
    the sandbox would have network access, so that case stays fail-closed.
    """
    if sys.platform != "linux":
        return False
    if not shutil.which("python3") and not sys.executable:
        return False
    if not _seccomp_available():
        return False
    return _landlock_abi() > 0 if _landlock_enabled() else True


async def execute_user_command(
    cmd: str,
    cwd: str | Path,
    timeout: float,
    username: str,
) -> ExecutionResult:
    """Run *cmd* in the calling account's sandbox.

    Raises :class:`UserSandboxError` when isolation cannot be installed.  The
    caller must treat that as a refusal, never as permission to run directly.
    """
    if not _seccomp_available():
        raise UserSandboxError(
            "容器缺少 libseccomp，无法阻止沙箱访问网络，出于安全考虑已拒绝执行。"
        )
    landlock_enabled = _landlock_enabled()
    landlock_abi = _landlock_abi()
    if landlock_enabled and landlock_abi < 1:
        raise UserSandboxError(
            "已开启 HUIZHI_SANDBOX_LANDLOCK=1，但当前内核没有 Landlock。"
            "请改用支持 Landlock 的内核，或将该配置设为 0 使用 UID/权限沙箱。"
        )
    permission_fallback = not landlock_enabled
    if permission_fallback:
        logger.info(
            "sandbox: HUIZHI_SANDBOX_LANDLOCK=0; using the UID/group "
            "permission sandbox.",
        )

    paths = await asyncio.to_thread(prepare_paths, username, permission_fallback)
    cwd_path = Path(cwd).expanduser()
    try:
        cwd_path = cwd_path.resolve()
    except OSError:
        cwd_path = paths.account_root
    if not _is_inside(cwd_path, paths.account_root) or not cwd_path.is_dir():
        cwd_path = paths.account_root

    env = build_safe_env(paths)
    env["HUIZHI_SANDBOX_MODE"] = "permission" if permission_fallback else "landlock"
    # Own-database access: a per-account PG role behind a UNIX-socket proxy.
    # The proxy only accepts that role/database pair, so the application's
    # superuser credentials cannot be used to cross tenant boundaries.  A
    # provisioning failure must not block file-only work; it simply runs
    # without PG* variables.
    try:
        from .db_access import (
            DbAccessError,
            ensure_account_db_access,
            pg_environment,
        )

        db_access = await ensure_account_db_access(username, paths)
        env.update(pg_environment(db_access))
        env["HUIZHI_DB_AVAILABLE"] = "1"
    except DbAccessError as exc:
        logger.warning("sandbox db: %s", exc)
        env["HUIZHI_DB_AVAILABLE"] = "0"
    except Exception:  # noqa: BLE001 - database setup must not block the shell
        logger.warning("sandbox db: provisioning failed", exc_info=True)
        env["HUIZHI_DB_AVAILABLE"] = "0"

    # External data/API gateway: the sandbox has no TCP network, so configured
    # third-party sources are reached through a UNIX-socket JSON gateway.
    try:
        from .external_gateway import ensure_external_gateway

        gateway = await ensure_external_gateway(
            username,
            paths,
            paths.identity.uid,
            paths.identity.gid,
        )
        env["HUIZHI_GATEWAY_SOCKET"] = str(gateway.socket_path)
        env["HUIZHI_GATEWAY_CLIENT"] = str(gateway.client_path)
    except Exception:  # noqa: BLE001 - external access is optional
        logger.warning("external gateway: startup failed", exc_info=True)

    script = _generate_runner_script(cmd, cwd_path, paths, timeout)

    fd, script_path = tempfile.mkstemp(prefix="huizhi_sandbox_", suffix=".py", dir="/tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(script)
        os.chmod(script_path, 0o700)

        python = sys.executable or "/usr/bin/python3"
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            python,
            script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(paths.account_root),
            env=env,
            start_new_session=True,
        )
        async def _kill_tree() -> None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass

        try:
            try:
                from ...tool_calls import cancellable_wait

                stdout_bytes, stderr_bytes = await cancellable_wait(
                    proc.communicate(),
                    fallback_secs=max(1.0, float(timeout)) if timeout else None,
                    as_kill_deadline=True,
                )
            except ImportError:  # pragma: no cover - tool_calls is part of app
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=max(1.0, float(timeout)) if timeout else None,
                )
            duration_ms = int((time.monotonic() - start) * 1000)
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            violation = None
            lowered = stderr.lower()
            if proc.returncode != 0 and (
                "sandbox setup failed" in lowered
                or "permission denied" in lowered
                or "operation not permitted" in lowered
                or "read-only file system" in lowered
            ):
                violation = stderr.strip()
            result = ExecutionResult(
                exit_code=proc.returncode or 0,
                stdout=stdout,
                stderr=stderr,
                timed_out=False,
                duration_ms=duration_ms,
                sandbox_violation=violation,
            )
            record_sandbox_execution(
                username,
                paths.identity.uid,
                str(cwd_path),
                cmd,
                result,
            )
            return result
        except asyncio.TimeoutError:
            duration_ms = int((time.monotonic() - start) * 1000)
            await _kill_tree()
            result = ExecutionResult(
                exit_code=-1,
                stdout="",
                stderr="Command timed out",
                timed_out=True,
                duration_ms=duration_ms,
                sandbox_violation="timeout",
            )
            record_sandbox_execution(
                username,
                paths.identity.uid,
                str(cwd_path),
                cmd,
                result,
            )
            return result
        except asyncio.CancelledError:
            await _kill_tree()
            record_sandbox_execution(
                username,
                paths.identity.uid,
                str(cwd_path),
                cmd,
                ExecutionResult(
                    exit_code=-1,
                    stdout="",
                    stderr="cancelled",
                    timed_out=False,
                    sandbox_violation="cancelled",
                ),
            )
            raise
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass
