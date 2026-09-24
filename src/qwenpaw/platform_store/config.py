# -*- coding: utf-8 -*-
"""Connection settings for the platform's own PostgreSQL store.

The platform keeps its own data (conversation history, governance audit, …) in
PostgreSQL rather than SQLite, so a deployment has one durable store to back up
and one place to reason about.

Connection resolution is layered so that operators only have to configure
credentials once:

1. ``QWENPAW_PLATFORM_DB_*`` — the platform store's own settings, when a
   deployment wants it on a separate server.
2. ``QWENPAW_DATA_TABLES_DB_*`` — the same PostgreSQL the data-analysis tables
   already use; this is the normal case, so it is the fallback.
3. The built-in defaults matching the bundled ``postgres:17-alpine`` container.

Every platform table lives in the ``platform`` schema, which keeps the platform's
own data separate from the knowledge service's tables and the per-account
analysis databases while sharing one connection pool.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Schema holding every platform-owned table. Fully qualifying table names (rather
# than relying on ``search_path``) keeps queries explicit and immune to a caller
# changing the session's schema.
PLATFORM_SCHEMA = "platform"

# Advisory-lock key serializing first-time schema creation across processes.
# Distinct from the knowledge service's key (2026082201) so the two services
# cannot block each other while booting.
SCHEMA_LOCK_KEY = 2026092401


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _layered(suffix: str, default: str) -> str:
    """Resolve one setting through the platform → data-tables → default chain."""
    for prefix in ("QWENPAW_PLATFORM_DB", "QWENPAW_DATA_TABLES_DB"):
        value = _env(f"{prefix}_{suffix}")
        if value:
            return value
    return default


@dataclass(frozen=True)
class PlatformDbConfig:
    """Resolved connection settings for the platform store."""

    host: str
    port: int
    dbname: str
    user: str
    password: str

    @property
    def host_candidates(self) -> list[str]:
        """Hosts to try in order.

        An explicitly configured host is used alone: silently falling back to a
        different server would connect to the wrong database. Only the built-in
        default tries the container gateway first, because that is where the
        bundled PostgreSQL lives under host networking.
        """
        configured = _env("QWENPAW_PLATFORM_DB_HOST") or _env(
            "QWENPAW_DATA_TABLES_DB_HOST",
        )
        if configured:
            return [configured]
        return ["host.docker.internal", "127.0.0.1"]

    def connect_kwargs(self, host: str) -> dict:
        """Connection kwargs for ``psycopg2`` against *host*."""
        return {
            "host": host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.user,
            "password": self.password,
            "connect_timeout": 10,
            # Bound anything a caller forgets to bound. History queries run on
            # a worker thread, so a runaway statement would otherwise pin a pool
            # slot indefinitely.
            "options": "-c statement_timeout=30000",
        }


def load_platform_db_config() -> PlatformDbConfig:
    """Read the platform store's connection settings from the environment."""
    return PlatformDbConfig(
        host=_env("QWENPAW_PLATFORM_DB_HOST") or _env("QWENPAW_DATA_TABLES_DB_HOST"),
        port=int(_layered("PORT", "15432")),
        dbname=_layered("NAME", "huizhi_knowledge"),
        user=_layered("USER", "huizhi_knowledge"),
        password=_layered("PASSWORD", "huizhi_knowledge"),
    )


__all__ = [
    "PLATFORM_SCHEMA",
    "SCHEMA_LOCK_KEY",
    "PlatformDbConfig",
    "load_platform_db_config",
]
