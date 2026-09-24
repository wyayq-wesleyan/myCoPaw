# -*- coding: utf-8 -*-
"""The platform's own PostgreSQL store.

The platform keeps its data in PostgreSQL rather than SQLite so that a
deployment has one durable store to operate, back up, and reason about — and so
that platform data is not scattered across per-workspace files that are awkward
to query, index, or reconcile after an upgrade.

Modules:

* :mod:`config` — connection resolution and the ``platform`` schema name.
* :mod:`pool`   — a small bounded pool that degrades instead of crashing.
* :mod:`schema` — versioned, advisory-locked, failure-isolated migrations.
* :mod:`legacy_import` — one-time move of pre-upgrade SQLite data into the store.

Everything is namespaced under the ``platform`` PostgreSQL schema, separate from
the knowledge service's tables and the per-account analysis databases, while
sharing one connection pool.
"""

from __future__ import annotations

from .config import PLATFORM_SCHEMA, PlatformDbConfig, load_platform_db_config
from .legacy_import import ImportReport, ImportResult, run_legacy_import
from .pool import (
    PlatformStoreUnavailable,
    dict_cursor,
    get_conn,
    is_available,
    last_error,
    put_conn,
    reset_pool,
)
from .schema import Migration, apply_migrations, ensure_schema

__all__ = [
    "ImportReport",
    "ImportResult",
    "Migration",
    "PLATFORM_SCHEMA",
    "PlatformDbConfig",
    "PlatformStoreUnavailable",
    "apply_migrations",
    "dict_cursor",
    "ensure_schema",
    "get_conn",
    "is_available",
    "last_error",
    "load_platform_db_config",
    "put_conn",
    "reset_pool",
    "run_legacy_import",
]
