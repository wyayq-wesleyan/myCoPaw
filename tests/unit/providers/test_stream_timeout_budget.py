# -*- coding: utf-8 -*-
"""The stream watchdogs must tolerate slow self-hosted models.

This deployment runs local models. Asked to continue a long agentic context,
they can spend minutes processing the prompt before the first token, and stall
for a long time again mid-generation. The original 30s defaults killed those
runs with "LLM stream ... produced no content", which users read as the product
being broken rather than the model being slow.

These tests pin the budget (5 minutes) and the fact that both phases share it,
so a future "tighten the timeout" edit cannot silently reintroduce the
false failure. The failure mode is genuinely hard to spot otherwise: the code
still "works", it just discards slow-but-healthy runs.
"""

import importlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPECTED_SECONDS = 300.0


@pytest.fixture
def fresh_constants(monkeypatch):
    """Reload ``constant`` so env-var edits are reflected in module globals."""
    monkeypatch.delenv("QWENPAW_LLM_STREAM_FIRST_CONTENT_TIMEOUT", raising=False)
    monkeypatch.delenv("QWENPAW_LLM_STREAM_IDLE_TIMEOUT", raising=False)
    monkeypatch.delenv("COPAW_LLM_STREAM_FIRST_CONTENT_TIMEOUT", raising=False)
    monkeypatch.delenv("COPAW_LLM_STREAM_IDLE_TIMEOUT", raising=False)
    import qwenpaw.constant as constant

    return importlib.reload(constant)


def test_first_content_budget_defaults_to_five_minutes(fresh_constants):
    assert fresh_constants.LLM_STREAM_FIRST_CONTENT_TIMEOUT == EXPECTED_SECONDS


def test_idle_budget_defaults_to_five_minutes(fresh_constants):
    assert fresh_constants.LLM_STREAM_IDLE_TIMEOUT == EXPECTED_SECONDS


def test_the_two_phases_share_one_budget(fresh_constants):
    """A shorter idle budget would just move the failure one phase later.

    Both phases wait on the same upstream, so the permissive budget has to
    apply to whichever phase the run is in.
    """
    assert (
        fresh_constants.LLM_STREAM_FIRST_CONTENT_TIMEOUT
        == fresh_constants.LLM_STREAM_IDLE_TIMEOUT
    )


def test_environment_can_lower_the_budget(monkeypatch):
    """The default is a ceiling, not a hard-coded value."""
    monkeypatch.setenv("QWENPAW_LLM_STREAM_FIRST_CONTENT_TIMEOUT", "45")
    import qwenpaw.constant as constant

    reloaded = importlib.reload(constant)
    try:
        assert reloaded.LLM_STREAM_FIRST_CONTENT_TIMEOUT == 45.0
        # Pairs with the default under test, so the reload above must not have
        # leaked the previous assertion's value into this one.
        assert reloaded.LLM_STREAM_IDLE_TIMEOUT == EXPECTED_SECONDS
    finally:
        monkeypatch.delenv("QWENPAW_LLM_STREAM_FIRST_CONTENT_TIMEOUT", raising=False)
        importlib.reload(constant)


# --------------------------------------------------------------------------- #
# Deployment surfaces must agree with the code default. A compose file still
# saying 120 pins the container to a stricter budget than the code promises,
# which is exactly the mismatch that produced the user-visible failure.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "relative_path",
    [
        "deploy/docker-compose.code-mount.amd64.yml",
        "deploy/docker-compose.code-mount.arm64.yml",
    ],
)
def test_compose_defaults_match_the_code(relative_path):
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    for var in (
        "QWENPAW_LLM_STREAM_FIRST_CONTENT_TIMEOUT",
        "QWENPAW_LLM_STREAM_IDLE_TIMEOUT",
    ):
        assert re.search(
            rf"\$\{{{var}:-300\}}",
            text,
        ), f"{relative_path} must default {var} to 300"


@pytest.mark.parametrize(
    "relative_path",
    [
        "deploy/.env.example",
        "deploy/full-offline/env/main.env.example",
    ],
)
def test_env_templates_match_the_code(relative_path):
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    for var in (
        "QWENPAW_LLM_STREAM_FIRST_CONTENT_TIMEOUT",
        "QWENPAW_LLM_STREAM_IDLE_TIMEOUT",
    ):
        assert re.search(
            rf"^{var}=300$",
            text,
            re.MULTILINE,
        ), f"{relative_path} must document {var}=300"
