"""A saved tool allowlist must not hide tools shipped by a later upgrade.

`account_tools` is a snapshot of the pool taken when an administrator last made
an explicit choice. A tool released afterwards is simply absent from it, and
treating absence as "disabled" silently removed every new tool from every
account that already had a selection — the tool then looked broken in chat with
no trace of why.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from qwenpaw.app import user_space


def _pool(names: list[str], enabled_by_default: bool = True) -> dict:
    return {
        name: SimpleNamespace(name=name, enabled=enabled_by_default)
        for name in names
    }


@pytest.fixture
def pool(monkeypatch):
    """Install a fake tool pool; the real one is large and config-driven."""
    def _install(names: list[str]) -> None:
        monkeypatch.setattr(
            user_space,
            "_tool_pool",
            lambda: _pool(names),
        )

    return _install


def test_tools_shipped_after_the_selection_are_enabled(pool):
    """The bug: a new tool was absent from the snapshot, not rejected."""
    pool(["read_file", "create_data_table"])

    enabled = user_space._account_tools_from_state(
        {"account_tools": ["read_file"]},
    )

    assert enabled == ["read_file", "create_data_table"]


def test_a_stale_state_is_permissive_by_necessity(pool):
    """Without the marker, "not listed" and "never existed" look identical.

    A state saved before the marker existed cannot distinguish a tool the
    administrator removed from one that had not shipped yet. The permissive
    reading is chosen deliberately: a hidden new tool is a silent breakage,
    whereas a re-enabled default-on tool is visible and re-settable.
    """
    pool(["alpha", "beta", "gamma"])

    enabled = user_space._account_tools_from_state(
        {"account_tools": ["gamma", "alpha"]},
    )

    assert enabled == ["alpha", "beta", "gamma"]


def test_explicitly_excluded_tool_stays_disabled(pool):
    """Once the marker is present, a deliberate removal is honoured exactly."""
    pool(["read_file", "create_data_table"])

    enabled = user_space._account_tools_from_state(
        {
            "account_tools": ["read_file"],
            "account_tools_known": ["read_file", "create_data_table"],
        },
    )

    assert enabled == ["read_file"]


def test_selected_tools_are_kept_exactly(pool):
    pool(["read_file", "write_file", "bash"])

    enabled = user_space._account_tools_from_state(
        {
            "account_tools": ["read_file", "bash"],
            "account_tools_known": ["read_file", "write_file", "bash"],
        },
    )

    assert enabled == ["read_file", "bash"]


def test_default_disabled_tool_is_not_swept_in(pool, monkeypatch):
    """Only default-on tools get the benefit of the doubt."""
    pool(["read_file", "plugin_tool"])
    # A plugin tool shipped default-off must stay off even though it is absent
    # from an old snapshot.
    monkeypatch.setattr(
        user_space,
        "_tool_pool",
        lambda: {
            "read_file": SimpleNamespace(name="read_file", enabled=True),
            "plugin_tool": SimpleNamespace(name="plugin_tool", enabled=False),
        },
    )

    enabled = user_space._account_tools_from_state(
        {"account_tools": ["read_file"]},
    )

    assert enabled == ["read_file"]


def test_output_follows_pool_order(pool):
    pool(["alpha", "beta", "gamma"])

    enabled = user_space._account_tools_from_state(
        {
            "account_tools": ["gamma", "alpha"],
            "account_tools_known": ["alpha", "beta", "gamma"],
        },
    )

    assert enabled == ["alpha", "gamma"]


# ---------------------------------------------------------------------------
# One-time repair for configs already written before the fix
# ---------------------------------------------------------------------------


class _StateFarm:
    """In-memory stand-in for the per-account state files."""

    def __init__(self, states: dict[str, dict]) -> None:
        self.states = states
        self.applied: list[tuple[str, list[str]]] = []


@pytest.fixture
def farm(monkeypatch):
    def _install(states: dict[str, dict], users: list[str] | None = None):
        store = _StateFarm(states)
        monkeypatch.setattr(
            user_space,
            "load_user_state",
            lambda username: dict(store.states.get(username, {})),
        )
        monkeypatch.setattr(
            user_space,
            "set_account_tools",
            lambda username, names: store.applied.append((username, list(names))),
        )
        import qwenpaw.app.auth as auth

        monkeypatch.setattr(
            auth,
            "list_users",
            lambda: [{"username": u} for u in (users or list(states))],
        )
        return store

    return _install


def test_repairs_a_state_saved_before_the_marker(pool, farm):
    pool(["read_file", "create_data_table"])
    store = farm({"alice": {"account_tools": ["read_file"]}})

    repaired = user_space.heal_account_tool_allowlists()

    assert repaired == 1
    assert store.applied == [("alice", ["read_file", "create_data_table"])]


def test_leaves_states_written_after_the_marker_alone(pool, farm):
    """Later, deliberate choices must never be overwritten."""
    pool(["read_file", "create_data_table"])
    store = farm(
        {
            "alice": {
                "account_tools": ["read_file"],
                "account_tools_known": ["read_file", "create_data_table"],
            },
        },
    )

    repaired = user_space.heal_account_tool_allowlists()

    assert repaired == 0
    assert store.applied == []


def test_ignores_accounts_with_no_saved_selection(pool, farm):
    """Not having chosen means the default applies; nothing to repair."""
    pool(["read_file"])
    store = farm({"alice": {"default_agent_id": "x"}})

    repaired = user_space.heal_account_tool_allowlists()

    assert repaired == 0
    assert store.applied == []


def test_repair_is_idempotent(pool, farm):
    pool(["read_file", "create_data_table"])
    store = farm({"alice": {"account_tools": ["read_file"]}})

    assert user_space.heal_account_tool_allowlists() == 1
    # The repair records the marker, so a second pass has nothing to do.
    store.states["alice"]["account_tools_known"] = ["read_file", "create_data_table"]
    assert user_space.heal_account_tool_allowlists() == 0
    assert len(store.applied) == 1


def test_one_failing_account_does_not_block_the_rest(pool, monkeypatch):
    pool(["read_file", "create_data_table"])

    applied: list[str] = []

    def _apply(username, names):
        if username == "alice":
            raise RuntimeError("state file unreadable")
        applied.append(username)

    monkeypatch.setattr(
        user_space,
        "load_user_state",
        lambda username: {"account_tools": ["read_file"]},
    )
    monkeypatch.setattr(user_space, "set_account_tools", _apply)
    import qwenpaw.app.auth as auth

    monkeypatch.setattr(
        auth,
        "list_users",
        lambda: [{"username": "alice"}, {"username": "bob"}],
    )

    repaired = user_space.heal_account_tool_allowlists()

    assert repaired == 1
    assert applied == ["bob"]


def test_unreadable_user_list_is_not_fatal(pool, monkeypatch):
    """Startup must continue even if the account list cannot be read."""
    pool(["read_file"])

    import qwenpaw.app.auth as auth

    def _boom():
        raise RuntimeError("auth store unavailable")

    monkeypatch.setattr(auth, "list_users", _boom)

    assert user_space.heal_account_tool_allowlists() == 0
