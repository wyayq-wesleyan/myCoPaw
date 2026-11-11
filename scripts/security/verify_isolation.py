#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end isolation verification for one ordinary account.

Run inside the application container as root:

    docker exec huizhi-assistant python /workspace/app/verify-isolation.py \
      --user isolation_probe --other isolation_probe_other --cleanup

It exercises the kernel sandbox, own-database access, cross-database refusal,
external gateway, resource limits and audit.  Exit code 0 means every required
check passed; skipped checks are reported separately.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shlex
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASS.append(name)
        print(f"PASS {name}")
    else:
        FAIL.append(name)
        print(f"FAIL {name} {detail}".rstrip())


def skip(name: str, detail: str = "") -> None:
    SKIP.append(name)
    print(f"SKIP {name} {detail}".rstrip())


def _text(result) -> str:
    return (result.stdout or "") + "\n" + (result.stderr or "")


def _setup_accounts(user: str, other: str):
    sys.path.insert(0, "/workspace/app/src")
    from qwenpaw.app.user_space import get_user_root

    root = get_user_root(user).expanduser().resolve()
    other_root = get_user_root(other).expanduser().resolve()
    for base in (root, other_root):
        for sub in ("media", "upload", "outputs", "scripts"):
            (base / sub).mkdir(parents=True, exist_ok=True)
    (root / "media" / "m.txt").write_text("isolation-media\n", "utf-8")
    (root / "upload" / "u.txt").write_text("isolation-upload\n", "utf-8")
    (root / "note.txt").write_text("isolation-note\n", "utf-8")
    (other_root / "secret.txt").write_text("other-private\n", "utf-8")
    (root.parent / "workspace-secret.txt").write_text("workspace-secret\n", "utf-8")
    return root, other_root


def _fs_command(root: Path, other_root: Path) -> str:
    q = shlex.quote
    return f'''
set +e
echo "OWN_MEDIA"; cat {q(str(root / "media" / "m.txt"))}; echo "OWN_MEDIA_END"
echo "OWN_UPLOAD"; cat {q(str(root / "upload" / "u.txt"))}; echo "OWN_UPLOAD_END"
echo "OWN_NOTE"; cat {q(str(root / "note.txt"))}; echo "OWN_NOTE_END"
echo "OUT_WRITE"; echo ok > {q(str(root / "outputs" / "verify.txt"))} && echo ok > {q(str(root / "scripts" / "verify.py"))} && echo OUT_WRITE_OK; echo "OUT_WRITE_END"
echo "OTHER"; cat {q(str(other_root / "secret.txt"))} 2>&1 || echo OTHER_BLOCKED; echo "OTHER_END"
echo "WS_SECRET"; cat {q(str(root.parent / "workspace-secret.txt"))} 2>&1 || echo WS_SECRET_BLOCKED; echo "WS_SECRET_END"
echo "APP_CODE"; cat /workspace/app/src/qwenpaw/__init__.py 2>&1 || echo APP_CODE_BLOCKED; echo "APP_CODE_END"
echo "PROC"; cat /proc/1/environ 2>&1 || echo PROC_BLOCKED; echo "PROC_END"
echo "SHADOW"; cat /etc/shadow 2>&1 || echo SHADOW_BLOCKED; echo "SHADOW_END"
echo "MEDIA_WRITE"; echo x > {q(str(root / "media" / "x"))} 2>&1 && echo MEDIA_WRITE_LEAK || echo MEDIA_WRITE_BLOCKED; echo "MEDIA_WRITE_END"
echo "UPLOAD_WRITE"; echo x > {q(str(root / "upload" / "x"))} 2>&1 && echo UPLOAD_WRITE_LEAK || echo UPLOAD_WRITE_BLOCKED; echo "UPLOAD_WRITE_END"
echo "ROOT_WRITE"; echo x > {q(str(root / "x"))} 2>&1 && echo ROOT_WRITE_LEAK || echo ROOT_WRITE_BLOCKED; echo "ROOT_WRITE_END"
echo "TMP_WRITE"; echo x > /tmp/verify 2>&1 && echo TMP_WRITE_LEAK || echo TMP_WRITE_BLOCKED; echo "TMP_WRITE_END"
echo "HARDLINK"; rm -f {q(str(root / "outputs" / "hardlink"))}; ln {q(str(root / "media" / "m.txt"))} {q(str(root / "outputs" / "hardlink"))} 2>&1 && echo HARDLINK_LEAK || echo HARDLINK_BLOCKED; echo "HARDLINK_END"
echo "SYMLINK"; rm -f {q(str(root / "outputs" / "symlink"))}; ln -s {q(str(root / "media" / "m.txt"))} {q(str(root / "outputs" / "symlink"))}; echo x > {q(str(root / "outputs" / "symlink"))} 2>&1 && echo SYMLINK_LEAK || echo SYMLINK_BLOCKED; echo "SYMLINK_END"
echo "TRUNCATE"; truncate -s 0 {q(str(root / "media" / "m.txt"))} 2>&1 && echo TRUNCATE_LEAK || echo TRUNCATE_BLOCKED; echo "TRUNCATE_END"
echo "UNLINK"; rm {q(str(root / "media" / "m.txt"))} 2>&1 && echo UNLINK_LEAK || echo UNLINK_BLOCKED; echo "UNLINK_END"
echo "RENAME"; mv {q(str(root / "media" / "m.txt"))} {q(str(root / "outputs" / "moved"))} 2>&1 && echo RENAME_LEAK || echo RENAME_BLOCKED; echo "RENAME_END"
echo "NET"; python -c "import socket; socket.socket(socket.AF_INET, socket.SOCK_DGRAM)" 2>&1 && echo NET_LEAK || echo NET_BLOCKED; echo "NET_END"
echo "KILL"; python -c "import os; os.kill(1, 0)" 2>&1 && echo KILL_LEAK || echo KILL_BLOCKED; echo "KILL_END"
echo "PTRACE"; python -c "import ctypes; libc=ctypes.CDLL(None); rc=libc.ptrace(16, 1, 0, 0); print('PTRACE_OK' if rc == 0 else 'PTRACE_DENIED')" 2>&1; echo "PTRACE_END"
echo "MOUNT"; mount -t tmpfs tmpfs {q(str(root / "outputs" / "mnt"))} 2>&1 && echo MOUNT_LEAK || echo MOUNT_BLOCKED; echo "MOUNT_END"
echo "UNSHARE"; unshare -m true 2>&1 && echo UNSHARE_LEAK || echo UNSHARE_BLOCKED; echo "UNSHARE_END"
echo "MKNOD"; mknod {q(str(root / "outputs" / "dev"))} c 1 3 2>&1 && echo MKNOD_LEAK || echo MKNOD_BLOCKED; echo "MKNOD_END"
echo "CHROOT"; chroot {q(str(root / "outputs"))} /bin/sh 2>&1 && echo CHROOT_LEAK || echo CHROOT_BLOCKED; echo "CHROOT_END"
'''


def _db_command() -> str:
    return r'''
python - <<'PY'
import os
print("DB_ENV", os.environ.get("HUIZHI_DB_AVAILABLE"), os.environ.get("PGDATABASE"))
if os.environ.get("HUIZHI_DB_AVAILABLE") != "1":
    print("DB_UNAVAILABLE")
    raise SystemExit(0)
import psycopg2

def attempt(label, fn):
    try:
        fn()
        print(label + "_LEAK")
    except Exception as exc:
        print(label + "_BLOCKED", type(exc).__name__)

try:
    conn = psycopg2.connect()
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("CREATE TEMP TABLE verify_t (note text)")
    cur.execute("INSERT INTO verify_t VALUES ('ok')")
    cur.execute("SELECT note FROM verify_t")
    print("DB_OWN_OK", cur.fetchone()[0])
    conn.rollback()
    conn.close()
except Exception as exc:
    print("DB_OWN_FAIL", type(exc).__name__, exc)

attempt("CROSS_DB", lambda: psycopg2.connect(dbname="huizhi_knowledge"))
attempt("APP_ROLE", lambda: psycopg2.connect(
    user="huizhi_knowledge", password="definitely-not-the-password",
    dbname=os.environ.get("PGDATABASE")))
attempt("TEMPLATE_DB", lambda: psycopg2.connect(dbname="template1"))
attempt("TCP_DIRECT", lambda: psycopg2.connect(
    host="127.0.0.1", port=15432, dbname=os.environ.get("PGDATABASE")))

try:
    conn = psycopg2.connect()
    conn.autocommit = True
    cur = conn.cursor()
    attempt("SET_ROLE", lambda: cur.execute("SET ROLE huizhi_knowledge"))
    attempt("READ_FILE", lambda: cur.execute("SELECT pg_read_file('/etc/passwd')"))
    attempt("COPY_PROGRAM", lambda: cur.execute("COPY (SELECT 1) TO PROGRAM 'id'"))
    attempt("EXTENSION", lambda: cur.execute("CREATE EXTENSION IF NOT EXISTS dblink"))
    conn.close()
except Exception as exc:
    print("DB_ADV_FAIL", type(exc).__name__, exc)
PY
'''


def _gateway_command(http_url: str | None) -> str:
    http_line = ""
    if http_url:
        http_line = (
            "try:\n"
            f"    r = huizhi_gateway.http('GET', {http_url!r})\n"
            "    print('GATEWAY_HTTP_OK', r.get('status'))\n"
            "except Exception as exc:\n"
            "    print('GATEWAY_HTTP_FAIL', type(exc).__name__, exc)\n"
        )
    return r'''
python - <<'PY'
import os, sys
if not os.environ.get("HUIZHI_GATEWAY_SOCKET"):
    print("GATEWAY_UNAVAILABLE")
    raise SystemExit(0)
sys.path.insert(0, os.environ["HUIZHI_OUTPUT_DIR"])
try:
    import huizhi_gateway
    sources = huizhi_gateway.sources()
    print("GATEWAY_SOURCES", sources)
    if not sources:
        print("GATEWAY_NO_SOURCES")
    else:
        name = sources[0]["name"]
        try:
            rows = huizhi_gateway.query(name, "SELECT 1 AS x")
            print("GATEWAY_QUERY_OK", rows.get("rows"))
        except Exception as exc:
            print("GATEWAY_QUERY_FAIL", type(exc).__name__, exc)
        try:
            huizhi_gateway.query(name, "INSERT INTO verify_gateway VALUES (1)")
            print("GATEWAY_WRITE_LEAK")
        except Exception as exc:
            print("GATEWAY_WRITE_BLOCKED", type(exc).__name__)
except Exception as exc:
    print("GATEWAY_INIT_FAIL", type(exc).__name__, exc)
''' + http_line + "PY\n"


def _http_server() -> tuple[HTTPServer, int]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"isolation-http-ok")

        def log_message(self, *args):
            pass

    try:
        server = HTTPServer(("127.0.0.1", 18080), Handler)
    except OSError:
        server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


async def _run(args) -> int:
    sys.path.insert(0, "/workspace/app/src")
    from qwenpaw.sandbox.user_isolation import execute_user_command

    root, other_root = _setup_accounts(args.user, args.other)
    result = await execute_user_command(
        _fs_command(root, other_root),
        root,
        args.timeout,
        args.user,
    )
    text = _text(result)
    print("\n--- filesystem/sandbox ---\n" + text[:12000])
    check("OWN_MEDIA_CONTENT", "isolation-media" in text)
    check("OWN_UPLOAD_CONTENT", "isolation-upload" in text)
    check("OWN_NOTE_CONTENT", "isolation-note" in text)
    check("OUT_WRITE", "OUT_WRITE_OK" in text)
    for marker in (
        "OTHER_BLOCKED",
        "WS_SECRET_BLOCKED",
        "APP_CODE_BLOCKED",
        "PROC_BLOCKED",
        "SHADOW_BLOCKED",
        "MEDIA_WRITE_BLOCKED",
        "UPLOAD_WRITE_BLOCKED",
        "ROOT_WRITE_BLOCKED",
        "TMP_WRITE_BLOCKED",
        "HARDLINK_BLOCKED",
        "SYMLINK_BLOCKED",
        "TRUNCATE_BLOCKED",
        "UNLINK_BLOCKED",
        "RENAME_BLOCKED",
        "NET_BLOCKED",
        "KILL_BLOCKED",
        "PTRACE_DENIED",
        "MOUNT_BLOCKED",
        "UNSHARE_BLOCKED",
        "MKNOD_BLOCKED",
        "CHROOT_BLOCKED",
    ):
        check(marker, marker in text and f"{marker.replace('_BLOCKED','')}_LEAK" not in text)

    db_result = await execute_user_command(_db_command(), root, args.timeout, args.user)
    db_text = _text(db_result)
    print("\n--- database ---\n" + db_text[:12000])
    if "DB_UNAVAILABLE" in db_text:
        skip("DB", "HUIZHI_DB_AVAILABLE=0")
    else:
        check("DB_OWN_OK", "DB_OWN_OK ok" in db_text)
        for marker in (
            "CROSS_DB_BLOCKED",
            "APP_ROLE_BLOCKED",
            "TEMPLATE_DB_BLOCKED",
            "TCP_DIRECT_BLOCKED",
            "SET_ROLE_BLOCKED",
            "READ_FILE_BLOCKED",
            "COPY_PROGRAM_BLOCKED",
            "EXTENSION_BLOCKED",
        ):
            check(marker, marker in db_text and marker.replace("_BLOCKED", "_LEAK") not in db_text)

    server = None
    http_url = None
    if not args.skip_http:
        server, port = _http_server()
        http_url = f"http://127.0.0.1:{port}/"
    try:
        gw_result = await execute_user_command(
            _gateway_command(http_url),
            root,
            args.timeout,
            args.user,
        )
        gw_text = _text(gw_result)
        print("\n--- external gateway ---\n" + gw_text[:12000])
        if "GATEWAY_UNAVAILABLE" in gw_text:
            skip("GATEWAY", "socket not configured")
        else:
            check("GATEWAY_SOURCES", "GATEWAY_SOURCES" in gw_text)
            if "GATEWAY_NO_SOURCES" in gw_text:
                skip("GATEWAY_QUERY", "no sources configured")
                skip("GATEWAY_WRITE", "no sources configured")
                skip("GATEWAY_HTTP", "no sources configured")
            else:
                check("GATEWAY_QUERY_OK", "GATEWAY_QUERY_OK" in gw_text)
                check("GATEWAY_WRITE_BLOCKED", "GATEWAY_WRITE_BLOCKED" in gw_text)
                if http_url:
                    check("GATEWAY_HTTP_OK", "GATEWAY_HTTP_OK" in gw_text)
    finally:
        if server is not None:
            server.shutdown()

    if not args.skip_resource:
        old_mem = os.environ.get("HUIZHI_SANDBOX_MEMORY_MB")
        old_file = os.environ.get("HUIZHI_SANDBOX_FILE_MB")
        try:
            os.environ["HUIZHI_SANDBOX_MEMORY_MB"] = "512"
            mem = await execute_user_command(
                "python -c \"b=bytearray(1024*1024*1024); print('MEM_LEAK')\" 2>&1 || echo MEM_BLOCKED",
                root,
                30,
                args.user,
            )
            mem_text = _text(mem)
            print("\n--- memory limit ---\n" + mem_text[:3000])
            check("MEM_LIMIT", "MEM_BLOCKED" in mem_text and "MEM_LEAK" not in mem_text)

            os.environ["HUIZHI_SANDBOX_FILE_MB"] = "1"
            file_result = await execute_user_command(
                "python -c \"open('outputs/too-big','wb').write(b'x'*(2*1024*1024)); print('FILE_LEAK')\" 2>&1 || echo FILE_BLOCKED",
                root,
                30,
                args.user,
            )
            file_text = _text(file_result)
            print("\n--- file limit ---\n" + file_text[:3000])
            check("FILE_LIMIT", "FILE_BLOCKED" in file_text and "FILE_LEAK" not in file_text)
        finally:
            if old_mem is None:
                os.environ.pop("HUIZHI_SANDBOX_MEMORY_MB", None)
            else:
                os.environ["HUIZHI_SANDBOX_MEMORY_MB"] = old_mem
            if old_file is None:
                os.environ.pop("HUIZHI_SANDBOX_FILE_MB", None)
            else:
                os.environ["HUIZHI_SANDBOX_FILE_MB"] = old_file

    timeout_result = await execute_user_command(
        "sleep 30",
        root,
        2,
        args.user,
    )
    check("TIMEOUT_KILL", timeout_result.timed_out)

    from qwenpaw.constant import WORKING_DIR

    audit = Path(WORKING_DIR) / "logs" / "sandbox-audit.jsonl"
    if audit.is_file():
        check(
            "AUDIT",
            f'"user": "{args.user}"' in audit.read_text("utf-8", errors="replace"),
        )
    else:
        check("AUDIT", False, str(audit))

    print("\n==== SUMMARY ====")
    print(f"PASS={len(PASS)} FAIL={len(FAIL)} SKIP={len(SKIP)}")
    for name in FAIL:
        print(f"  FAILED: {name}")
    for name in SKIP:
        print(f"  SKIPPED: {name}")

    if args.cleanup:
        _cleanup(args.user)
    return 1 if FAIL else 0


def _cleanup(user: str) -> None:
    try:
        import psycopg2
        from qwenpaw.sandbox.db_access import _account_database, _role_name, _secret_path

        db = _account_database(user)
        role = _role_name(user)
        host = os.environ.get("QWENPAW_DATA_TABLES_DB_HOST", "127.0.0.1")
        port = int(os.environ.get("QWENPAW_DATA_TABLES_DB_PORT", "15432"))
        app_user = os.environ.get("QWENPAW_DATA_TABLES_DB_USER", "huizhi_knowledge")
        app_pw = os.environ.get("QWENPAW_DATA_TABLES_DB_PASSWORD", "huizhi_knowledge")
        conn = psycopg2.connect(
            host=host, port=port, dbname="postgres", user=app_user,
            password=app_pw, connect_timeout=5,
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (db,))
        cur.execute(f'DROP DATABASE IF EXISTS "{db}"')
        cur.execute(f'DROP ROLE IF EXISTS "{role}"')
        conn.close()
        try:
            _secret_path(user).unlink()
        except OSError:
            pass
        print(f"CLEANUP removed {db} / {role}")
    except Exception as exc:  # noqa: BLE001
        print(f"CLEANUP skipped: {type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default="isolation_probe")
    parser.add_argument("--other", default="isolation_probe_other")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--skip-resource", action="store_true")
    parser.add_argument("--skip-http", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    if args.cleanup and not (
        args.user.startswith("isolation_probe")
        or args.user.startswith("verify_")
    ):
        print(
            "--cleanup would DROP the account database/role; only allowed for "
            "a probe account (isolation_probe* / verify_*).",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
