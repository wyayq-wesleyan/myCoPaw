# -*- coding: utf-8 -*-
"""UT for OFF-mode sandbox provisioning.

approval_level=OFF skips "ask the user" but must NOT skip "run it in a
sandbox". These tests pin that only fail-closed tools get a ``sandbox_config``
compiled in OFF mode, that fail-open shell tools (Bash) are left untouched, and
that no sandbox platform → no-op.

The mechanism is generic: it applies to any tool the registry flags
``sandbox_required``. That flag has no built-in member today — the sandboxed
``recall_history_python`` REPL it was written for was removed when history
moved into the shared PostgreSQL store — so these tests register their own
fail-closed tool rather than relying on one shipped in the catalog.
"""
from __future__ import annotations

# pylint: disable=protected-access

from types import SimpleNamespace

import pytest

from qwenpaw.governance import tool_adapter
from qwenpaw.governance.resource_governor import ResourceGovernor
from qwenpaw.governance.tool_registry import (
    DEFAULT_REGISTRY,
    register_tool_governance,
)

# A stand-in fail-closed tool: name → policy name is PascalCase, so the tool is
# named ``python_repl_probe`` and the policy identity is ``PythonReplProbe``.
_PROBE_TOOL = "python_repl_probe"
_PROBE_POLICY = "PythonReplProbe"


@pytest.fixture(autouse=True)
def _probe_fail_closed_tool():
    """Register a sandbox-required builtin for the duration of each test."""
    register_tool_governance(
        DEFAULT_REGISTRY,
        python_name=_PROBE_TOOL,
        tool_type="shell",
        target_param="source",
        policy_name=_PROBE_POLICY,
        sandbox_required=True,
        owner="builtin",
    )
    yield
    DEFAULT_REGISTRY.unregister_python_tool(_PROBE_TOOL)


class _FakeGovernor:
    def __init__(
        self,
        sandbox_available: bool = True,
        sandbox_enabled: bool = True,
    ) -> None:
        self.sandbox_available = sandbox_available
        self._sandbox_enabled = sandbox_enabled
        self.compiled: list = []

    @property
    def sandbox_usable(self) -> bool:
        """Mirror ResourceGovernor: platform support AND global switch."""
        return self.sandbox_available and self._sandbox_enabled

    def compile_sandbox_config(self, tc_spec):  # noqa: ANN
        self.compiled.append(tc_spec)
        return f"sandbox-cfg-for-{tc_spec}"


class _FakeTool:
    """Minimal stand-in for a PolicyGuardedTool instance."""

    def __init__(self, name: str) -> None:
        self.name = name

    def _build_tc_spec(self):  # noqa: ANN
        return f"tc:{self.name}"


class TestRegistryFlag:
    def test_probe_tool_requires_sandbox(self):
        assert DEFAULT_REGISTRY.requires_sandbox(_PROBE_POLICY) is True

    def test_bash_does_not_require_sandbox(self):
        assert DEFAULT_REGISTRY.requires_sandbox("Bash") is False

    def test_unknown_tool_defaults_false(self):
        assert DEFAULT_REGISTRY.requires_sandbox("NopeTool") is False


class TestOffModeSandbox:
    def test_fail_closed_tool_gets_sandbox_compiled(self):
        tool = _FakeTool(_PROBE_TOOL)
        gov = _FakeGovernor(sandbox_available=True)

        tool_adapter._prepare_off_mode_sandbox(tool, gov)

        assert tool._qp_sandbox_mode is True
        assert tool._qp_sandbox_config == f"sandbox-cfg-for-tc:{_PROBE_TOOL}"
        assert gov.compiled, "compile_sandbox_config was never called"

    def test_bash_left_untouched(self):
        """Fail-open Bash must stay unsandboxed in OFF mode."""
        tool = _FakeTool("execute_shell_command")
        gov = _FakeGovernor(sandbox_available=True)

        tool_adapter._prepare_off_mode_sandbox(tool, gov)

        assert not hasattr(tool, "_qp_sandbox_mode")
        assert not hasattr(tool, "_qp_sandbox_config")
        assert not gov.compiled

    def test_no_sandbox_platform_is_noop(self):
        """No sandbox available → config stays unset."""
        tool = _FakeTool(_PROBE_TOOL)
        gov = _FakeGovernor(sandbox_available=False)

        tool_adapter._prepare_off_mode_sandbox(tool, gov)

        assert not hasattr(tool, "_qp_sandbox_mode")
        assert not hasattr(tool, "_qp_sandbox_config")
        assert not gov.compiled

    def test_sandbox_switch_off_is_noop(self):
        """sandbox_enabled=false must skip OFF-mode provisioning too.

        Even when the platform supports a sandbox, an explicit global
        ``sandbox_enabled=false`` means the user opted out — the OFF-mode
        path must honour that just like the normal policy path, leaving the
        tool unsandboxed rather than silently forcing a sandbox on it.
        """
        tool = _FakeTool(_PROBE_TOOL)
        gov = _FakeGovernor(sandbox_available=True, sandbox_enabled=False)

        tool_adapter._prepare_off_mode_sandbox(tool, gov)

        assert not hasattr(tool, "_qp_sandbox_mode")
        assert not hasattr(tool, "_qp_sandbox_config")
        assert not gov.compiled

    def test_sandbox_switch_off_clears_previous_config(self):
        """A hot switch-off must not reuse per-call state from an earlier
        sandboxed invocation on the same reusable tool wrapper.
        """
        tool = _FakeTool(_PROBE_TOOL)
        gov = _FakeGovernor(sandbox_available=True, sandbox_enabled=True)

        tool_adapter._prepare_off_mode_sandbox(tool, gov)
        assert tool._qp_sandbox_mode is True
        assert hasattr(tool, "_qp_sandbox_config")

        gov._sandbox_enabled = False
        tool_adapter._prepare_off_mode_sandbox(tool, gov)

        assert not hasattr(tool, "_qp_sandbox_mode")
        assert not hasattr(tool, "_qp_sandbox_config")
        assert len(gov.compiled) == 1

    def test_no_governor_is_noop(self):
        tool = _FakeTool(_PROBE_TOOL)
        tool_adapter._prepare_off_mode_sandbox(tool, None)
        assert not hasattr(tool, "_qp_sandbox_mode")

    def test_compile_failure_leaves_config_unset(self):
        """A compile error must fail closed, not run unsandboxed."""

        class _BoomGovernor(_FakeGovernor):
            def compile_sandbox_config(self, tc_spec):  # noqa: ANN
                raise RuntimeError("boom")

        tool = _FakeTool(_PROBE_TOOL)
        gov = _BoomGovernor(sandbox_available=True)

        tool_adapter._prepare_off_mode_sandbox(tool, gov)

        # sandbox_mode is only set AFTER a successful compile, so it stays
        # unset — the tool's own fail-closed guard then denies the call.
        assert not hasattr(tool, "_qp_sandbox_mode")


class TestSandboxSwitchHotReload:
    """The global ``sandbox_enabled`` switch must take effect without a
    restart.

    ``_prepare_off_mode_sandbox`` reads ``governor.sandbox_usable``, which
    combines the one-time platform probe with the live config switch. Flipping
    the switch on an already-started governor must change ``sandbox_usable`` on
    the very next read — no ``governor.start()`` / process restart required
    (the switch is read through the mtime-cached ``load_config``).
    """

    @staticmethod
    def _governor_with_platform_sandbox(tmp_path) -> ResourceGovernor:
        gov = ResourceGovernor(
            workspace_dir=str(tmp_path),
            governance_dir=str(tmp_path / "gov"),
        )
        # Simulate a successful platform probe from start().
        gov._sandbox_available = True
        return gov

    @staticmethod
    def _patch_switch(monkeypatch, state: dict) -> None:
        import qwenpaw.config as config_mod

        monkeypatch.setattr(
            config_mod,
            "load_config",
            lambda *a, **k: SimpleNamespace(
                security=SimpleNamespace(
                    sandbox_enabled=state["value"],
                ),
            ),
        )

    def test_sandbox_usable_follows_switch_without_restart(
        self,
        tmp_path,
        monkeypatch,
    ):
        state = {"value": True}
        self._patch_switch(monkeypatch, state)

        gov = self._governor_with_platform_sandbox(tmp_path)
        assert gov.sandbox_usable is True

        # Operator flips the switch off (PUT /security/sandbox) — no restart.
        state["value"] = False
        assert gov.sandbox_usable is False

        # ...and back on again, still the same governor instance.
        state["value"] = True
        assert gov.sandbox_usable is True

    def test_off_mode_provisioning_skipped_when_switch_off(
        self,
        tmp_path,
        monkeypatch,
    ):
        """End-to-end with a real governor: OFF-mode provisioning is skipped
        once the switch is off, even though the platform supports a sandbox.
        """
        state = {"value": False}
        self._patch_switch(monkeypatch, state)

        gov = self._governor_with_platform_sandbox(tmp_path)
        tool = _FakeTool(_PROBE_TOOL)

        tool_adapter._prepare_off_mode_sandbox(tool, gov)

        assert not hasattr(tool, "_qp_sandbox_mode")
        assert not hasattr(tool, "_qp_sandbox_config")
