# -*- coding: utf-8 -*-
"""The knowledge service must verify who is calling, not trust a header.

Deployment context: the API listens on the host network (port 18100), so any
process on the LAN can reach it. Before this change ``X-User-Id`` was the whole
identity, and ``X-Is-Admin`` a client-controlled privilege flag. The main app now
signs ``username\\ntimestamp`` with a shared secret; the service verifies the
signature and the clock skew before touching the store.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_APP = pathlib.Path(__file__).resolve().parents[3] / "knowledge-service" / "app"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"ks_{name}", _APP / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


identity = _load("identity")


def test_sign_and_verify_round_trip():
    ts, sig = identity.sign_identity("s3cret", "alice", timestamp=1_700_000_000)

    ok, reason = identity.verify_identity(
        "s3cret", "alice", ts, sig, now=1_700_000_000,
    )
    assert ok, reason


def test_another_users_signature_is_rejected():
    ts, sig = identity.sign_identity("s3cret", "alice", timestamp=1_700_000_000)

    ok, reason = identity.verify_identity(
        "s3cret", "bob", ts, sig, now=1_700_000_000,
    )
    assert not ok
    assert "signature" in reason


def test_wrong_secret_is_rejected():
    ts, sig = identity.sign_identity("s3cret", "alice", timestamp=1_700_000_000)

    ok, _ = identity.verify_identity(
        "other", "alice", ts, sig, now=1_700_000_000,
    )
    assert not ok


def test_expired_signature_is_rejected():
    ts, sig = identity.sign_identity("s3cret", "alice", timestamp=1_700_000_000)

    ok, reason = identity.verify_identity(
        "s3cret",
        "alice",
        ts,
        sig,
        now=1_700_000_000 + identity.DEFAULT_MAX_SKEW_SECONDS + 1,
    )
    assert not ok
    assert "skew" in reason


def test_missing_fields_are_rejected_when_a_secret_is_configured():
    for ts, sig in ((None, "x"), (1_700_000_000, None), (None, None)):
        ok, _ = identity.verify_identity("s3cret", "alice", ts, sig)
        assert not ok


def test_legacy_mode_only_when_no_secret_is_configured():
    """An unset secret is a deliberate degraded mode, not a free pass."""

    ok, reason = identity.verify_identity("", "alice", None, None)
    assert ok
    assert "not configured" in reason
