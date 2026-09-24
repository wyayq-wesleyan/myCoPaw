# -*- coding: utf-8 -*-
"""Schema migrations for the platform's own PostgreSQL tables.

One ordered list per concern, each versioned so an upgrade is auditable and
re-runnable. Adding a table or column means appending a new :class:`Migration`
with a fresh version — never editing an applied one, because it will already
have run on deployed databases.

Versions are ``YYYYMMDDNNNN`` so the ordering reads as a date and the list stays
append-only.
"""

from __future__ import annotations

from .schema import Migration

# --- conversation history -------------------------------------------------
#
# Replaces the per-workspace ``history.db`` SQLite files. Two structural
# differences from that schema matter:
#
# * Rows from every workspace share one table, keyed by ``workspace_key``. The
#   old ``seq`` was unique within one file and is addressed on its own by the
#   eviction index and by ``update_entry``/``existing_seqs`` — so ``seq`` stays
#   per-workspace and is allocated from ``history_seq`` rather than being a
#   global identity. A global sequence would renumber existing history and
#   invalidate the ``seq`` ranges already persisted in session state.
#
# * There is no inverted index to maintain. SQLite's FTS5 needed explicit
#   insert/delete upkeep on every write; PostgreSQL maintains the GIN index
#   below itself, which removes a whole class of "index drifted from the table"
#   bugs. ``gin_trgm_ops`` is used because trigram matching handles CJK
#   substrings well — FTS5's ``unicode61`` tokenizer effectively split Chinese
#   per character.
HISTORY_MIGRATIONS: list[Migration] = [
    Migration(
        "202609240001",
        "conversation_history",
        (
            "CREATE EXTENSION IF NOT EXISTS pg_trgm",
            """
            CREATE TABLE IF NOT EXISTS "platform".history_seq (
                workspace_key TEXT PRIMARY KEY,
                last_seq      BIGINT NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS "platform".conversation_history (
                workspace_key TEXT   NOT NULL,
                seq           BIGINT NOT NULL,
                session_id    TEXT   NOT NULL,
                agent_id      TEXT,
                kind          TEXT   NOT NULL,
                role          TEXT,
                name          TEXT,
                content       TEXT,
                tool_call_id  TEXT,
                tool_input    TEXT,
                tool_state    TEXT,
                headline      TEXT,
                blocks        TEXT,
                metadata      TEXT,
                created_at    TEXT,
                dedup_key     TEXT,
                PRIMARY KEY (workspace_key, seq)
            )
            """,
            # Same access shapes the old per-file indexes served, now scoped by
            # workspace so one table can hold every workspace.
            'CREATE INDEX IF NOT EXISTS ch_session ON "platform".conversation_history (workspace_key, session_id)',
            'CREATE INDEX IF NOT EXISTS ch_agent ON "platform".conversation_history (workspace_key, agent_id)',
            'CREATE INDEX IF NOT EXISTS ch_kind ON "platform".conversation_history (workspace_key, kind)',
            'CREATE INDEX IF NOT EXISTS ch_created_at ON "platform".conversation_history (workspace_key, created_at)',
            # Idempotency net, carried over unchanged: a repeated append of the
            # same logical event collides and is dropped. A NULL dedup_key never
            # conflicts (SQL NULLs are distinct in a unique index), so un-keyed
            # rows are never deduped — the same contract as before.
            'CREATE UNIQUE INDEX IF NOT EXISTS ux_dedup ON "platform".conversation_history (workspace_key, session_id, dedup_key)',
            # Keyword search path. pg_trgm GIN accelerates LIKE/ILIKE, which is
            # what the recall search falls back to today; ranking is computed in
            # SQL over the matched rows.
            'CREATE INDEX IF NOT EXISTS ch_content_trgm ON "platform".conversation_history USING GIN (content gin_trgm_ops)',
        ),
    ),
]

# --- governance audit -----------------------------------------------------
#
# Replaces the global ``governance/audit.db``. The old file was shared by every
# account (a multi-user hazard: one process's writes were visible to all, and
# the only isolation was a column in the row). The workspace column is kept and
# indexed so the same data can be filtered per account.
AUDIT_MIGRATIONS: list[Migration] = [
    Migration(
        "202609240002",
        "governance_audit",
        (
            """
            CREATE TABLE IF NOT EXISTS "platform".audit_events (
                id           BIGSERIAL PRIMARY KEY,
                ts           DOUBLE PRECISION NOT NULL,
                workspace_dir TEXT,
                agent_id     TEXT,
                session_id   TEXT,
                tool_name    TEXT,
                target       TEXT,
                decision     TEXT,
                reason       TEXT,
                extra        TEXT
            )
            """,
            'CREATE INDEX IF NOT EXISTS audit_ts ON "platform".audit_events (ts)',
            'CREATE INDEX IF NOT EXISTS audit_workspace ON "platform".audit_events (workspace_dir)',
            'CREATE INDEX IF NOT EXISTS audit_tool ON "platform".audit_events (tool_name)',
        ),
    ),
]

# --- upgrade bookkeeping --------------------------------------------------
#
# Records which legacy SQLite sources have been imported into PostgreSQL, so the
# import runs once per file and can resume after an interruption. A checksum
# lets a later run detect that a source changed since it was imported.
IMPORT_STATE_MIGRATIONS: list[Migration] = [
    Migration(
        "202609240003",
        "legacy_import_state",
        (
            """
            CREATE TABLE IF NOT EXISTS "platform".legacy_import_state (
                source_path  TEXT NOT NULL,
                kind         TEXT NOT NULL,
                checksum     TEXT,
                row_count    BIGINT NOT NULL DEFAULT 0,
                status       TEXT NOT NULL,
                detail       TEXT,
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (source_path, kind)
            )
            """,
            'CREATE INDEX IF NOT EXISTS legacy_import_status ON "platform".legacy_import_state (kind, status)',
        ),
    ),
]

# --- token usage ----------------------------------------------------------
#
# Replaces the global ``token_usage.json``. That file was one shared document for
# every account, rewritten in full on each flush: concurrent writers could lose
# each other's increments, and the per-account breakdown lived as a nested dict
# that could only be read by loading everything. One row per
# (date, provider, model, account) is directly queryable and increments
# atomically.
#
# ``user_id = ''`` means "unattributed" — usage recorded before the account
# dimension existed. It is deliberately a real key rather than NULL so the
# primary key's uniqueness applies to it (NULLs would not conflict).
TOKEN_USAGE_MIGRATIONS: list[Migration] = [
    Migration(
        "202609240004",
        "token_usage",
        (
            """
            CREATE TABLE IF NOT EXISTS "platform".token_usage (
                used_on            TEXT NOT NULL,
                provider_id        TEXT NOT NULL DEFAULT '',
                model_name         TEXT NOT NULL,
                user_id            TEXT NOT NULL DEFAULT '',
                prompt_tokens      BIGINT NOT NULL DEFAULT 0,
                completion_tokens  BIGINT NOT NULL DEFAULT 0,
                call_count         BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (used_on, provider_id, model_name, user_id)
            )
            """,
            'CREATE INDEX IF NOT EXISTS token_usage_user ON "platform".token_usage (user_id, used_on)',
            'CREATE INDEX IF NOT EXISTS token_usage_date ON "platform".token_usage (used_on)',
        ),
    ),
]

# Everything the platform store owns, applied in one ordered pass at startup.
ALL_MIGRATIONS: list[Migration] = [
    *HISTORY_MIGRATIONS,
    *AUDIT_MIGRATIONS,
    *IMPORT_STATE_MIGRATIONS,
    *TOKEN_USAGE_MIGRATIONS,
]

__all__ = [
    "ALL_MIGRATIONS",
    "AUDIT_MIGRATIONS",
    "HISTORY_MIGRATIONS",
    "IMPORT_STATE_MIGRATIONS",
    "TOKEN_USAGE_MIGRATIONS",
]
