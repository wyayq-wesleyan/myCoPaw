# -*- coding: utf-8 -*-
"""One-time upgrade into path scoping.

Before paths existed, an account's knowledge documents and data tables were one flat
bucket, and every agent in that account could reach all of it. Path scoping adds
a second isolation dimension *inside* an account (separate cases must not see each
other). This module makes that upgrade a no-op from the user's point of view.

The migration is deliberately small, because the model does most of the work:

* **Unbound resources are treated as belonging to the account's root path.** So
  "isolate this material" means "bind it to a child path" — and everything that
  existed before paths were introduced stays public without being enumerated.
  That matters because the resources live in three different places (knowledge
  service behind HTTP, data tables in per-account databases, spaces in the
  knowledge database); enumerating them at upgrade time would be fragile, and
  anything missed would silently vanish from every agent's view.

* **Every agent is granted the root path.** That is exactly the visibility it had
  before (everything was one bucket), so nothing disappears on upgrade. Child
  paths are *not* granted — which is what makes isolation effective from the first
  new case onwards.

So the upgrade is: ensure each account has a root path, and grant it to each of
that account's agents. Both steps are idempotent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import paths as path_store

logger = logging.getLogger(__name__)


@dataclass
class PathMigrationReport:
    """Outcome of one migration pass."""

    accounts: int = 0
    agents_granted: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"path-migration: {self.accounts} account(s), "
            f"{self.agents_granted} agent binding(s) ensured, "
            f"{len(self.errors)} error(s)"
        )


def _iter_account_agents() -> dict[str, list[str]]:
    """Map account name → its agent ids, from the live agent profiles.

    Read from the config rather than from the path tables: at upgrade time the
    path tables are empty, and the point is to learn *which agents exist* so each
    one keeps its visibility. An agent whose owner cannot be resolved is reported
    rather than guessed at — defaulting it to ``admin`` would grant it the wrong
    account's material.
    """
    from ..app.user_space import owner_for_agent
    from ..config import load_config

    try:
        config = load_config()
    except Exception:  # noqa: BLE001 - no config means nothing to migrate
        logger.debug("path-migration: config unavailable", exc_info=True)
        return {}

    mapping: dict[str, list[str]] = {}
    for agent_id in config.agents.profiles:
        try:
            owner = owner_for_agent(agent_id)
        except Exception:  # noqa: BLE001 - one bad profile must not stop the rest
            logger.debug(
                "path-migration: could not resolve owner for %s",
                agent_id,
                exc_info=True,
            )
            owner = None
        if not owner:
            continue
        mapping.setdefault(owner, []).append(agent_id)
    return mapping


def _iter_accounts() -> list[str]:
    """Every registered account, whether or not it owns an agent.

    The agent list alone is not enough: an account can exist with no agents (it was
    created but never used, or its agent was deleted). Such an account still needs
    its public area — otherwise its path tree renders empty, which is the exact
    symptom of "a new user has no knowledge/data tree".
    """
    try:
        from ..app.auth import list_users

        return [str(item["username"]) for item in list_users() if item.get("username")]
    except Exception:  # noqa: BLE001 - fall back to the agent-derived list
        logger.debug("path-migration: could not enumerate accounts", exc_info=True)
        return []


def migrate_into_path_scoping() -> PathMigrationReport:
    """Ensure each account has a root path and its agents are granted it.

    Runs on every startup. It is cheap (two idempotent statements per agent) and
    self-healing: an agent created while the platform was on the old version — or
    one whose config appeared later — is picked up on the next boot.

    Covers **all** registered accounts, not just those owning an agent, because the
    root path is what makes an account's tree non-empty. Before this, an account
    with no agents showed an empty tree on both pages (the reported bug).

    Never raises. It runs on the startup path, so a failure degrades to a log line;
    the consequence of skipping is only that a new agent starts with no grants
    (fail-closed), which the operator can fix by granting a path in the UI.
    """
    report = PathMigrationReport()
    try:
        mapping = _iter_account_agents()
    except Exception:  # noqa: BLE001 - startup path
        logger.warning("path-migration: could not enumerate agents", exc_info=True)
        report.errors.append("enumerate agents")
        return report

    # Accounts with no agents have no entry in ``mapping``; add them with an empty
    # agent list so they still get a root path.
    for account in _iter_accounts():
        mapping.setdefault(account, [])

    for owner, agent_ids in mapping.items():
        try:
            path_store.ensure_root_path(owner)
        except Exception as exc:  # noqa: BLE001 - isolate per account
            logger.warning(
                "path-migration: could not create root path for %s: %s", owner, exc,
            )
            report.errors.append(f"root path for {owner}")
            continue
        report.accounts += 1
        for agent_id in agent_ids:
            try:
                # Granting the root only. Child paths stay ungranted, which is what
                # makes a newly created case invisible until it is explicitly
                # attached to an agent.
                path_store.grant_root_to_agent(owner, agent_id)
                report.agents_granted += 1
            except Exception as exc:  # noqa: BLE001 - isolate per agent
                logger.warning(
                    "path-migration: could not grant root to %s: %s", agent_id, exc,
                )
                report.errors.append(f"grant {agent_id}")

    if report.accounts or report.errors:
        logger.info("%s", report.summary())
    else:
        logger.debug("path-migration: no accounts to migrate")
    return report


__all__ = ["PathMigrationReport", "migrate_into_path_scoping"]
