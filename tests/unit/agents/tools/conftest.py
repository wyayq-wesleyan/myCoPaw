"""Shared fixtures for the tool unit tests.

These tools now confine every path to the calling account's workspace (see
``user_files.ensure_inside_account``). That check matters because in this
deployment the governance policy engine never runs (approval is hard-disabled)
and the sandbox is off, so it is the only thing keeping one account's files
away from another's.

Tool unit tests are about tool *behaviour*, not isolation, and they build their
fixtures in pytest's temporary directories — which sit outside any account root.
This fixture gives them an account root that contains those temporary paths so
they keep testing what they were written to test. Tests that ARE about
isolation opt out with ``pytest.mark.real_account_scope``.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _workspace_scope_for_tool_tests(request, monkeypatch):
    """Run behaviour-only tool tests without an account boundary.

    The root is the filesystem itself, which is the honest description: these
    tests exercise tool behaviour and deliberately do not model isolation, so
    every path they use is "inside the account".

    Skipped for tests marked ``real_account_scope``, which exercise the
    confinement logic itself and need the real per-account resolution.
    """
    if request.node.get_closest_marker("real_account_scope"):
        return

    from qwenpaw.agents.tools import user_files

    monkeypatch.setattr(
        user_files,
        "account_root",
        lambda _username=None: Path(Path("/").anchor or "/"),
    )
