# -*- coding: utf-8 -*-
"""End-to-end: a normal account's real chat turn is attributed to itself.

Every earlier check of the token-usage isolation exercised the *read* side with
hand-seeded events, which cannot catch a mismatch between the account key the
recording path writes and the one the reader filters on. This drives the real
path instead: authentication on, a second (non-admin) account, a real chat turn
against a mock LLM, then both the on-disk ``by_user`` key and the account's own
``/api/token-usage/details`` response are inspected.

The failure this pins is the reported symptom: a normal account opens the page
and always sees "no token consumption in the selected range", because its turns
were booked under a different key than the one the reader looks up.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import HTTPServer
from pathlib import Path
from typing import Iterator

import httpx
import pytest

from helpers import (
    MOCK_LLM_PROVIDER_ID,
    MockLLMHandler,
    default_http_timeout,
)

_HTTP_TIMEOUT = default_http_timeout(15.0)
_ADMIN = "e2e-admin"
_ADMIN_PASSWORD = "e2e-admin-pass-12345"
_BOB = "e2e-bob"
_BOB_PASSWORD = "e2e-bob-pass-12345"


def _find_free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        sock.listen(1)
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def mock_llm() -> Iterator[tuple[HTTPServer, str]]:
    srv = HTTPServer(("127.0.0.1", 0), MockLLMHandler)
    srv.force_error = False  # type: ignore[attr-defined]
    srv.force_tool_call = False  # type: ignore[attr-defined]
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, f"http://127.0.0.1:{port}/v1"
    srv.shutdown()


@pytest.fixture(scope="module")
def server(tmp_path_factory, mock_llm) -> Iterator[dict]:
    """Boot an auth-enabled app with an admin and a second normal account."""
    tmp_path = tmp_path_factory.mktemp("tu_e2e")
    host = "127.0.0.1"
    port = _find_free_port(host)

    working_dir = tmp_path / "working"
    secret_dir = tmp_path / "working.secret"
    backups_dir = tmp_path / "working.backups"
    for path in (working_dir, secret_dir, backups_dir):
        path.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DASHSCOPE_API_KEY"):
        env.pop(key, None)
    env["QWENPAW_WORKING_DIR"] = str(working_dir)
    env["QWENPAW_SECRET_DIR"] = str(secret_dir)
    env["QWENPAW_BACKUP_DIR"] = str(backups_dir)
    env["QWENPAW_AUTH_ENABLED"] = "true"
    env["QWENPAW_AUTH_USERNAME"] = _ADMIN
    env["QWENPAW_AUTH_PASSWORD"] = _ADMIN_PASSWORD
    # Production forces token auth even on loopback; mirror it so a missing
    # Authorization header cannot be masked by a localhost exemption.
    env["QWENPAW_FORCE_AUTH_ON_LOCALHOST"] = "true"
    env["QWENPAW_UPLOAD_MAX_SIZE_MB"] = "10"
    env["QWENPAW_RUNNING_IN_CONTAINER"] = "true"
    env["NO_PROXY"] = "*"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    logs: list[str] = []
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "qwenpaw",
            "app",
            "--host",
            host,
            "--port",
            str(port),
            "--log-level",
            "info",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert process.stdout is not None

    def _tee(stream, buf):
        for line in stream:
            buf.append(line)

    threading.Thread(
        target=_tee,
        args=(process.stdout, logs),
        daemon=True,
    ).start()

    client = httpx.Client(timeout=_HTTP_TIMEOUT, trust_env=False)
    base = f"http://{host}:{port}"

    def _tail() -> str:
        return "".join(logs)[-4000:]

    try:
        deadline = time.time() + 90.0
        ready = False
        while time.time() < deadline:
            if process.poll() is not None:
                raise AssertionError(
                    f"app exited during startup\n{_tail()}",
                )
            try:
                if client.get(f"{base}/api/version").status_code == 200:
                    ready = True
                    break
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            time.sleep(0.5)
        if not ready:
            raise AssertionError(f"app not ready in time\n{_tail()}")

        yield {
            "base": base,
            "client": client,
            "working_dir": working_dir,
            "logs": logs,
            "tail": _tail,
            "mock_url": mock_llm[1],
            "mock_srv": mock_llm[0],
        }
    finally:
        client.close()
        try:
            if sys.platform != "win32":
                process.send_signal(2)
            else:
                process.terminate()
            process.wait(timeout=15)
        except Exception:
            process.kill()
            process.wait(timeout=5)
        shutil.rmtree(tmp_path, ignore_errors=True)


def _login(server, username, password) -> str:
    resp = server["client"].post(
        f"{server['base']}/api/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, resp.text + server["tail"]()
    return resp.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register_provider(server, admin_token, mock_url) -> None:
    client = server["client"]
    base = server["base"]
    client.post(
        f"{base}/api/models/custom-providers",
        headers=_auth(admin_token),
        json={
            "id": MOCK_LLM_PROVIDER_ID,
            "name": "E2E Mock",
            "default_base_url": mock_url,
            "chat_model": "OpenAIChatModel",
            "models": [{"id": "mock-model", "name": "Mock Model"}],
        },
    )
    client.put(
        f"{base}/api/models/{MOCK_LLM_PROVIDER_ID}/config",
        headers=_auth(admin_token),
        json={"api_key": "test-key-mock", "base_url": mock_url},
    )
    client.put(
        f"{base}/api/models/active",
        headers=_auth(admin_token),
        json={
            "provider_id": MOCK_LLM_PROVIDER_ID,
            "model": "mock-model",
            "scope": "global",
        },
    )


def _create_user(server, admin_token, username, password) -> None:
    resp = server["client"].post(
        f"{server['base']}/api/auth/users",
        headers=_auth(admin_token),
        json={
            "username": username,
            "password": password,
            "role": "user",
        },
    )
    assert resp.status_code in (200, 201), resp.text + server["tail"]()


def _wait_task(server, token, task_id, timeout=90.0) -> dict:
    client = server["client"]
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        resp = client.get(
            f"{server['base']}/api/console/chat/task/{task_id}",
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text + server["tail"]()
        last = resp.json()
        if last.get("status") == "finished":
            return last
        time.sleep(0.3)
    pytest.fail(f"task did not finish: {last}\n{server['tail']()}")


def test_normal_account_sees_its_own_usage_e2e(server) -> None:
    """A real turn by a normal account is reported back to that account."""
    admin_token = _login(server, _ADMIN, _ADMIN_PASSWORD)
    _create_user(server, admin_token, _BOB, _BOB_PASSWORD)
    _register_provider(server, admin_token, server["mock_url"])
    bob_token = _login(server, _BOB, _BOB_PASSWORD)

    # Bob runs a real turn. The account channel must be bob, so the recorded
    # event lands under by_user["e2e-bob"], not a session/agent placeholder.
    submit = server["client"].post(
        f"{server['base']}/api/console/chat/task",
        headers=_auth(bob_token),
        json={
            "channel": "console",
            "user_id": "default",
            "session_id": "e2e-tu-bob-session",
            "input": [
                {
                    "role": "user",
                    "type": "message",
                    "content": [{"type": "text", "text": "hello"}],
                },
            ],
        },
    )
    assert submit.status_code == 200, submit.text + server["tail"]()
    task_id = submit.json()["task_id"]
    finished = _wait_task(server, bob_token, task_id)
    assert finished["result"]["status"] == "completed", (
        finished["result"],
        server["tail"](),
    )

    # Give the buffer's enqueue → disk flush a moment.
    data_file = server["working_dir"] / "token_usage.json"
    deadline = time.time() + 20.0
    payload = {}
    while time.time() < deadline:
        if data_file.exists():
            payload = json.loads(data_file.read_text(encoding="utf-8"))
            if payload:
                # Any entry carrying by_user means the event was consumed.
                if any(
                    isinstance(entry, dict) and entry.get("by_user")
                    for day in payload.values()
                    if isinstance(day, dict)
                    for entry in day.values()
                ):
                    break
        time.sleep(0.5)

    today = time.strftime("%Y-%m-%d")
    # Diagnostics: the unfiltered view folds in still-queued events, so it
    # shows whether the turn produced a usage event at all.
    admin_accounts = server["client"].get(
        f"{server['base']}/api/token-usage/accounts",
        headers=_auth(admin_token),
        params={"start_date": today, "end_date": today},
    )
    mock_calls = getattr(server.get("mock_srv"), "request_count", None)
    assert admin_accounts.status_code == 200, admin_accounts.text
    global_view = admin_accounts.json()
    assert global_view.get("by_user") or global_view.get("total_calls"), (
        f"no usage recorded at all: mock_calls={mock_calls} "
        f"admin_view={json.dumps(global_view, ensure_ascii=False)[:1500]} "
        f"file={json.dumps(payload, ensure_ascii=False)[:800]}"
    )

    keys = sorted(
        {
            user
            for day in payload.values()
            if isinstance(day, dict)
            for entry in day.values()
            if isinstance(entry, dict)
            for user in (entry.get("by_user") or {})
        },
    )
    assert _BOB in keys, (
        f"expected usage attributed to {_BOB!r}; by_user keys={keys}; "
        f"payload={json.dumps(payload, ensure_ascii=False)[:2000]}"
    )

    # And the account's own endpoint must return it.
    details = server["client"].get(
        f"{server['base']}/api/token-usage/details",
        headers=_auth(bob_token),
        params={"start_date": today, "end_date": today},
    )
    assert details.status_code == 200, details.text + server["tail"]()
    records = details.json()
    assert records, (
        f"{_BOB} saw no usage despite {_BOB!r} being in by_user; "
        f"payload={json.dumps(payload, ensure_ascii=False)[:2000]}"
    )
    assert sum(r.get("call_count", 0) for r in records) >= 1
