# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""The builder's scroll wiring gate and the tools it registers.

``_scroll_recall_runnable`` — whether scroll is wired at all. The structured
``recall_history`` tool runs in-process, but it is still guard-wrapped — with no
governor the guard layer itself is degraded. So the gate stays conservative:
governor absent and ``allow_unsandboxed`` off → degrade to native context
management rather than evict history nothing can read back.

Registration itself is now single-path: only the structured ``recall_history``
tool is offered. The sandboxed ``recall_history_python`` REPL was removed when
history moved into the platform's shared PostgreSQL store — a tool running
model-authored SQL could read every tenant's rows, and PostgreSQL has no
per-connection authorizer to stop it.
"""

from types import SimpleNamespace

from qwenpaw.runtime.builder import AgentBuilder


def _agent_config(allow_unsandboxed: bool) -> SimpleNamespace:
    return SimpleNamespace(
        running=SimpleNamespace(
            light_context_config=SimpleNamespace(
                scroll_config=SimpleNamespace(
                    allow_unsandboxed=allow_unsandboxed,
                ),
            ),
        ),
    )


def test_runnable_when_governor_present():
    # Governor injects sandbox_config, so recall runs regardless of the flag.
    cfg = _agent_config(allow_unsandboxed=False)
    assert AgentBuilder._scroll_recall_runnable(cfg, governor=object()) is True


def test_runnable_when_unsandboxed_opt_in(monkeypatch):
    # No governor, but the deployment opted into unsandboxed recall — which
    # needs BOTH the env var and the per-agent flag (agent.json alone can't
    # bypass the sandbox).
    monkeypatch.setenv("QWENPAW_ALLOW_UNSANDBOXED_RECALL", "1")
    cfg = _agent_config(allow_unsandboxed=True)
    assert AgentBuilder._scroll_recall_runnable(cfg, governor=None) is True


def test_not_runnable_without_governor_or_optin():
    # The reported failure: governor down, no opt-in → recall cannot run.
    cfg = _agent_config(allow_unsandboxed=False)
    assert AgentBuilder._scroll_recall_runnable(cfg, governor=None) is False


def test_not_runnable_on_malformed_config():
    # Missing scroll_config must fail closed (degrade to native), not raise.
    cfg = SimpleNamespace(running=SimpleNamespace(light_context_config=None))
    assert AgentBuilder._scroll_recall_runnable(cfg, governor=None) is False


def _governor(sandbox_available: bool) -> SimpleNamespace:
    return SimpleNamespace(sandbox_available=sandbox_available)


def _identity_wrap(fn, *_args, **_kwargs):
    # Bypass the real guard wrapper so we can assert on the raw tools that
    # were registered.
    return fn


def _scroll(recall_tool) -> SimpleNamespace:
    return SimpleNamespace(recall_tool=recall_tool)


def test_only_the_structured_tool_is_registered(monkeypatch):
    monkeypatch.setattr(
        AgentBuilder,
        "_wrap_tool",
        staticmethod(_identity_wrap),
    )
    builder = AgentBuilder.__new__(AgentBuilder)
    extra: list = []
    builder._append_scroll_recall_tools(
        extra,
        _scroll("RECALL_HISTORY"),
        _agent_config(allow_unsandboxed=False),
        "ag1",
        {},
        _governor(sandbox_available=False),
    )
    # Exactly one tool — the structured recall_history — regardless of whether
    # a sandbox exists.
    assert extra == ["RECALL_HISTORY"]


def test_registration_is_sandbox_independent(monkeypatch):
    """A sandbox's presence no longer changes which recall tools are offered.

    It used to gate the REPL. Since that tool is gone, the registered set must
    be identical with and without a sandbox.
    """
    monkeypatch.setattr(
        AgentBuilder,
        "_wrap_tool",
        staticmethod(_identity_wrap),
    )
    builder = AgentBuilder.__new__(AgentBuilder)

    with_sandbox: list = []
    builder._append_scroll_recall_tools(
        with_sandbox,
        _scroll("RECALL_HISTORY"),
        _agent_config(allow_unsandboxed=False),
        "ag1",
        {},
        _governor(sandbox_available=True),
    )
    without_sandbox: list = []
    builder._append_scroll_recall_tools(
        without_sandbox,
        _scroll("RECALL_HISTORY"),
        _agent_config(allow_unsandboxed=False),
        "ag1",
        {},
        _governor(sandbox_available=False),
    )
    assert with_sandbox == without_sandbox == ["RECALL_HISTORY"]


def test_no_repl_gate_remains():
    """The REPL gate is gone, not merely unused."""
    assert not hasattr(AgentBuilder, "_scroll_repl_runnable")
