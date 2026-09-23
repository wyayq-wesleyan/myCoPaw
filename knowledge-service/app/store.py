from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

logger = logging.getLogger("knowledge-store")

# Statements are logged through their own logger so the SQL stream can be turned
# on or off independently of the service's own logs.
sql_logger = logging.getLogger("knowledge-sql")


class LoggingConnection(psycopg.Connection):
    """A connection that logs the statements executed through it.

    ``psycopg`` 3 logs connection attempts and transactions but **not** the SQL
    it runs, at any level, so the statement text has to come from here. Overriding
    ``execute`` catches both explicit calls on the connection and statements
    issued through ``connection.cursor()`` (a cursor's ``execute`` delegates to
    the connection), which is a single choke point for every query in this module.

    Parameters are logged only at DEBUG: at INFO they would bury the statement
    under large ``IN``/batch parameter lists.
    """

    def execute(self, query, params=None, **kwargs):
        if sql_logger.isEnabledFor(logging.INFO):
            compact = " ".join(str(query).split())
            if sql_logger.isEnabledFor(logging.DEBUG):
                sql_logger.info("SQL: %s | params=%s", compact, params)
            else:
                sql_logger.info("SQL: %s", compact)
        return super().execute(query, params, **kwargs)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class Store:
    """PostgreSQL-backed durable store for parsed knowledge and retrieval data."""

    # 连接池容量按 30-50 并发用户评估：单次检索最多占用 2-3 个连接，
    # 20 上限可覆盖峰值并留余量；PostgreSQL 默认 max_connections=100。
    POOL_MIN_SIZE = 2
    POOL_MAX_SIZE = 20

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise RuntimeError("KNOWLEDGE_DATABASE_URL must point to PostgreSQL 17")
        self.database_url = database_url
        self._init_lock = threading.Lock()
        self._pool = None
        try:
            from psycopg_pool import ConnectionPool

            self._pool = ConnectionPool(
                database_url,
                min_size=self.POOL_MIN_SIZE,
                max_size=self.POOL_MAX_SIZE,
                open=True,
                # Both pooled and direct connections must log their statements;
                # passing the class here covers the pool, and ``_connect`` below
                # covers the no-pool fallback.
                connection_class=LoggingConnection,
                kwargs={"row_factory": dict_row},
                name="knowledge-store-pool",
            )
        except ImportError:
            # psycopg_pool 未安装时退回直连模式（开发环境可用）
            pass
        self._init_db_with_retry()

    def _init_db_with_retry(self, attempts: int = 20, delay: float = 3.0) -> None:
        """Initialize the schema, waiting out a PostgreSQL that is still starting.

        PostgreSQL runs as its own Compose project, so recreating the stack can
        bring this process up before the database accepts connections. Retry the
        idempotent DDL instead of exiting and relying on the restart policy.
        """
        for attempt in range(1, attempts + 1):
            try:
                self._init_db()
                return
            except Exception as exc:
                if attempt == attempts:
                    raise
                logger.warning(
                    "schema init attempt %d/%d failed, retrying in %.0fs: %s",
                    attempt,
                    attempts,
                    delay,
                    exc,
                )
                time.sleep(delay)

    def _connect(self):
        """Return a connection context manager; pooled when available."""
        if self._pool is not None:
            return self._pool.connection()
        return LoggingConnection.connect(
            self.database_url, row_factory=dict_row,
        )

    def _init_db(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS spaces (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              owner_id TEXT NOT NULL,
              created_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS documents (
              id TEXT PRIMARY KEY,
              space_id TEXT NOT NULL REFERENCES spaces(id),
              owner_id TEXT NOT NULL,
              filename TEXT NOT NULL,
              path TEXT NOT NULL,
              media_type TEXT,
              status TEXT NOT NULL,
              error TEXT,
              processing_stage TEXT,
              backend TEXT,
              content_hash TEXT,
              size BIGINT DEFAULT 0,
              created_at TIMESTAMPTZ NOT NULL,
              updated_at TIMESTAMPTZ NOT NULL,
              started_at TIMESTAMPTZ
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY,
              document_id TEXT NOT NULL REFERENCES documents(id),
              status TEXT NOT NULL,
              stage TEXT,
              attempts INTEGER NOT NULL DEFAULT 0,
              error TEXT,
              log_excerpt TEXT,
              next_retry_at TIMESTAMPTZ,
              created_at TIMESTAMPTZ NOT NULL,
              updated_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS worker_heartbeats (
              worker_id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              job_id TEXT,
              stage TEXT,
              updated_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS chunks (
              id TEXT PRIMARY KEY,
              document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
              space_id TEXT NOT NULL,
              sequence INTEGER NOT NULL,
              page INTEGER,
              location TEXT,
              content TEXT NOT NULL,
              source_type TEXT NOT NULL DEFAULT 'text',
              search_vector TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS entities (
              id TEXT PRIMARY KEY,
              space_id TEXT NOT NULL,
              name TEXT NOT NULL,
              entity_type TEXT NOT NULL,
              attributes_json JSONB NOT NULL DEFAULT '{}'::jsonb,
              UNIQUE(space_id, name, entity_type)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS mentions (
              id TEXT PRIMARY KEY,
              entity_id TEXT NOT NULL REFERENCES entities(id),
              chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
              role TEXT,
              confidence DOUBLE PRECISION
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS relations (
              id TEXT PRIMARY KEY,
              space_id TEXT NOT NULL,
              subject_id TEXT NOT NULL REFERENCES entities(id),
              predicate TEXT NOT NULL,
              object_id TEXT NOT NULL REFERENCES entities(id),
              evidence_chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
              confidence DOUBLE PRECISION
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS chunk_embeddings (
              chunk_id TEXT PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
              vector_json JSONB NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_documents_space ON documents(space_id)",
            "CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_chunks_space_document ON chunks(space_id, document_id, sequence)",
            "CREATE INDEX IF NOT EXISTS idx_chunks_search_vector ON chunks USING GIN(search_vector)",
            "CREATE INDEX IF NOT EXISTS idx_mentions_entity ON mentions(entity_id)",
            "CREATE INDEX IF NOT EXISTS idx_relations_subject ON relations(subject_id)",
            "CREATE INDEX IF NOT EXISTS idx_relations_object ON relations(object_id)",
        ]
        with self._init_lock, self._connect() as db:
            # The API and worker containers start together. A transaction-level
            # advisory lock keeps their first-run schema creation serialized.
            db.execute("SELECT pg_advisory_xact_lock(2026082201)")
            for statement in statements:
                db.execute(statement)
            # A database created by an older release predates some columns. A
            # failed statement aborts the surrounding PostgreSQL transaction, so
            # letting one failure escape silently skipped every later migration
            # -- that is how jobs.stage stayed missing after an upgrade while
            # the job queries kept failing. Each statement now runs in its own
            # savepoint so the rest still apply, and failures are logged instead
            # of swallowed.
            self._migrate(
                db,
                [
                    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS size BIGINT DEFAULT 0",
                    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ",
                    # parser backend is written after a document is parsed.
                    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS backend TEXT",
                    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash TEXT",
                    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS processing_stage TEXT",
                    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS log_excerpt TEXT",
                    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ",
                    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS stage TEXT",
                ],
            )

    @staticmethod
    def _migrate(db: Any, statements: list[str]) -> None:
        """Apply additive DDL, isolating each failure in its own savepoint."""
        for statement in statements:
            try:
                with db.transaction():
                    db.execute(statement)
            except Exception as exc:
                logger.warning("schema migration skipped (%s): %s", exc, statement)

    def create_space(self, name: str, owner_id: str) -> dict[str, Any]:
        space_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute(
                "INSERT INTO spaces (id, name, owner_id, created_at) VALUES (%s, %s, %s, %s)",
                (space_id, name, owner_id, now_utc()),
            )
        return {"id": space_id, "name": name, "owner_id": owner_id}

    def list_spaces(self, owner_id: str, is_admin: bool = False) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM spaces WHERE owner_id = %s ORDER BY created_at",
                (owner_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def space_exists(self, space_id: str, owner_id: str, is_admin: bool = False) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM spaces WHERE id = %s AND owner_id = %s",
                (space_id, owner_id),
            ).fetchone()
        return row is not None

    def create_document(
        self,
        space_id: str | None,
        owner_id: str,
        filename: str,
        path: str,
        media_type: str | None,
        document_id: str | None = None,
        size: int = 0,
        content_hash: str | None = None,
    ) -> dict[str, Any]:
        if space_id is None:
            space_id = self.get_or_create_user_space(owner_id)["id"]
        document_id = document_id or str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        timestamp = now_utc()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO documents
                  (id, space_id, owner_id, filename, path, media_type, status, error, processing_stage, content_hash, size, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'queued', NULL, 'queued', %s, %s, %s, %s)
                """,
                (document_id, space_id, owner_id, filename, path, media_type, content_hash, size, timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO jobs (id, document_id, status, stage, attempts, error, created_at, updated_at)
                VALUES (%s, %s, 'queued', 'queued', 0, NULL, %s, %s)
                """,
                (job_id, document_id, timestamp, timestamp),
            )
        return {"id": document_id, "job_id": job_id, "status": "queued", "filename": filename}

    def find_document_by_hash(
        self, space_id: str, owner_id: str, content_hash: str, size: int
    ) -> dict[str, Any] | None:
        """Return an existing document for an identical account-scoped upload."""
        with self._connect() as db:
            row = db.execute(
                """
                SELECT d.*, j.id AS job_id, j.status AS job_status
                FROM documents d
                LEFT JOIN LATERAL (
                    SELECT id, status FROM jobs
                    WHERE document_id = d.id ORDER BY created_at DESC, id DESC LIMIT 1
                ) j ON TRUE
                WHERE d.space_id = %s AND d.owner_id = %s
                  AND d.content_hash = %s AND d.size = %s
                ORDER BY d.created_at DESC, d.id DESC
                LIMIT 1
                """,
                (space_id, owner_id, content_hash, size),
            ).fetchone()
        return dict(row) if row else None

    def list_documents_internal(self, space_id: str) -> list[dict[str, Any]]:
        """List documents for trusted maintenance tasks, bypassing HTTP scopes."""
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT d.*, j.id AS job_id, j.status AS job_status,
                       j.attempts AS job_attempts, j.error AS job_error
                FROM documents d
                LEFT JOIN LATERAL (
                    SELECT id, status, attempts, error FROM jobs
                    WHERE document_id = d.id ORDER BY created_at DESC, id DESC LIMIT 1
                ) j ON TRUE
                WHERE d.space_id = %s
                ORDER BY d.created_at DESC
                """,
                (space_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_document(self, document_id: str, owner_id: str, is_admin: bool = False) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM documents WHERE id = %s AND owner_id = %s",
                (document_id, owner_id),
            ).fetchone()
        return dict(row) if row else None

    def get_document_stats(self, document_id: str, owner_id: str, is_admin: bool = False) -> dict[str, Any] | None:
        """返回文档的统计信息：分片数、实体数、关系数等。"""
        document = self.get_document(document_id, owner_id, is_admin)
        if not document:
            return None
        with self._connect() as db:
            chunks_row = db.execute(
                "SELECT count(*) AS cnt FROM chunks WHERE document_id = %s", (document_id,)
            ).fetchone()
            entities_row = db.execute(
                """SELECT count(*) AS cnt FROM entities e
                   JOIN mentions m ON m.entity_id = e.id
                   JOIN chunks c ON c.id = m.chunk_id
                   WHERE c.document_id = %s""", (document_id,)
            ).fetchone()
            relations_row = db.execute(
                """SELECT count(*) AS cnt FROM relations r
                   JOIN chunks c ON c.id = r.evidence_chunk_id
                   WHERE c.document_id = %s""", (document_id,)
            ).fetchone()
        return {
            "document_id": document_id,
            "filename": document["filename"],
            "status": document["status"],
            "processing_stage": document.get("processing_stage"),
            "error": document.get("error"),
            "size": document.get("size", 0),
            "chunk_count": chunks_row["cnt"] if chunks_row else 0,
            "entity_count": entities_row["cnt"] if entities_row else 0,
            "relation_count": relations_row["cnt"] if relations_row else 0,
            "created_at": document["created_at"].isoformat() if document.get("created_at") else None,
            "started_at": document["started_at"].isoformat() if document.get("started_at") else None,
            "completed_at": document["updated_at"].isoformat() if document.get("status") == "completed" else None,
            "processing_duration": (
                (document["updated_at"] - document["started_at"]).total_seconds()
                if document.get("started_at") and document.get("status") in ("completed", "failed")
                else None
            ),
        }

    def delete_document(self, document_id: str, owner_id: str, is_admin: bool = False) -> str:
        """Delete a document and related data.

        Returns ``deleted``, ``not_found`` or ``processing`` so the API can
        avoid racing the worker and report the correct outcome to callers.
        """
        document = self.get_document(document_id, owner_id, is_admin)
        if not document:
            return "not_found"
        if document.get("status") == "processing":
            return "processing"
        import os
        with self._connect() as db:
            db.execute(
                "DELETE FROM relations WHERE evidence_chunk_id IN (SELECT id FROM chunks WHERE document_id = %s)",
                (document_id,),
            )
            db.execute(
                "DELETE FROM mentions WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id = %s)",
                (document_id,),
            )
            db.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
            db.execute("DELETE FROM jobs WHERE document_id = %s", (document_id,))
            db.execute("DELETE FROM documents WHERE id = %s", (document_id,))
            # Entities are shared within a space. Remove only entities that
            # became unreachable after this document's mentions/relations were
            # deleted; entities still referenced by another document remain.
            db.execute(
                """
                DELETE FROM relations r
                WHERE r.space_id = %s
                  AND (
                    NOT EXISTS (SELECT 1 FROM mentions m WHERE m.entity_id = r.subject_id)
                    OR NOT EXISTS (SELECT 1 FROM mentions m WHERE m.entity_id = r.object_id)
                  )
                """,
                (document["space_id"],),
            )
            db.execute(
                """
                DELETE FROM entities e
                WHERE e.space_id = %s
                  AND NOT EXISTS (SELECT 1 FROM mentions m WHERE m.entity_id = e.id)
                  AND NOT EXISTS (
                    SELECT 1 FROM relations r
                    WHERE r.subject_id = e.id OR r.object_id = e.id
                )
                """,
                (document["space_id"],),
            )
        try:
            file_path = document.get("path", "")
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
        except OSError:
            pass
        return "deleted"

    def list_documents(self, space_id: str, owner_id: str, is_admin: bool = False) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT d.*, j.id AS job_id, j.status AS job_status,
                       j.attempts AS job_attempts, j.error AS job_error
                FROM documents d
                LEFT JOIN LATERAL (
                    SELECT id, status, attempts, error
                    FROM jobs
                    WHERE document_id = d.id
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                ) j ON TRUE
                WHERE d.space_id = %s AND d.owner_id = %s
                ORDER BY d.created_at DESC
                """,
                (space_id, owner_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_job(self, job_id: str, owner_id: str, is_admin: bool = False) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT j.*, d.owner_id, d.filename, d.space_id
                FROM jobs j JOIN documents d ON d.id = j.document_id
                WHERE j.id = %s AND d.owner_id = %s
                """,
                (job_id, owner_id),
            ).fetchone()
        return dict(row) if row else None

    def get_document_by_id_for_worker(self, document_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM documents WHERE id = %s", (document_id,)).fetchone()
        return dict(row) if row else None

    def claim_job(self) -> dict[str, Any] | None:
        """Claim one queued job safely across multiple worker processes."""
        timestamp = now_utc()
        with self._connect() as db:
            row = db.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'queued'
                  AND (next_retry_at IS NULL OR next_retry_at <= now())
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE jobs SET status = 'processing', stage = 'parsing', attempts = attempts + 1, updated_at = %s WHERE id = %s",
                (timestamp, row["id"]),
            )
            db.execute(
                "UPDATE documents SET status = 'processing', processing_stage = 'parsing', started_at = COALESCE(started_at, %s), updated_at = %s WHERE id = %s",
                (timestamp, timestamp, row["document_id"]),
            )
            row["attempts"] = int(row.get("attempts") or 0) + 1
            row["stage"] = "parsing"
        return dict(row)

    def update_job_stage(self, job_id: str, document_id: str, stage: str) -> None:
        """Persist the current worker phase so long tasks remain observable."""
        timestamp = now_utc()
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET stage = %s, updated_at = %s WHERE id = %s",
                (stage, timestamp, job_id),
            )
            db.execute(
                "UPDATE documents SET processing_stage = %s, updated_at = %s WHERE id = %s",
                (stage, timestamp, document_id),
            )

    def touch_worker_heartbeat(
        self,
        worker_id: str,
        status: str,
        job_id: str | None = None,
        stage: str | None = None,
    ) -> None:
        """Record worker liveness independently from a potentially long parse."""
        with self._connect() as db:
            db.execute(
                """INSERT INTO worker_heartbeats (worker_id, status, job_id, stage, updated_at)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (worker_id) DO UPDATE SET
                     status = EXCLUDED.status,
                     job_id = EXCLUDED.job_id,
                     stage = EXCLUDED.stage,
                     updated_at = EXCLUDED.updated_at""",
                (worker_id, status, job_id, stage, now_utc()),
            )

    def get_worker_health(self, max_age_seconds: int = 30) -> dict[str, Any]:
        """Return whether at least one worker has reported in recently."""
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM worker_heartbeats ORDER BY updated_at DESC LIMIT 1",
            ).fetchone()
        if not row:
            return {"available": False, "reason": "知识处理 Worker 尚未启动或未连接到当前数据库"}
        value = dict(row)
        updated_at = value.get("updated_at")
        age = (now_utc() - updated_at).total_seconds() if updated_at else float("inf")
        available = age <= max_age_seconds
        return {
            "available": available,
            "status": value.get("status"),
            "job_id": value.get("job_id"),
            "stage": value.get("stage"),
            "updated_at": updated_at.isoformat() if updated_at else None,
            "age_seconds": round(age, 1),
            "reason": None if available else "知识处理 Worker 心跳已超时，请检查 worker 容器和数据库连接",
        }

    def recover_interrupted_jobs(self) -> int:
        """Requeue jobs left in processing state by a process/container stop."""
        timestamp = now_utc()
        with self._connect() as db:
            row = db.execute(
                """
                UPDATE jobs SET status = 'queued', stage = 'queued', updated_at = %s,
                    next_retry_at = NULL,
                    error = COALESCE(error, '任务因服务重启中断，已自动恢复')
                WHERE status = 'processing'
                RETURNING id
                """,
                (timestamp,),
            ).fetchall()
            db.execute(
                """UPDATE documents SET status = 'queued', processing_stage = 'queued', updated_at = %s
                 WHERE status = 'processing'""",
                (timestamp,),
            )
        return len(row)

    def finish_job(self, job_id: str, document_id: str) -> None:
        timestamp = now_utc()
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET status = 'completed', stage = 'completed', updated_at = %s, error = NULL WHERE id = %s",
                (timestamp, job_id),
            )
            db.execute(
                "UPDATE documents SET status = 'completed', processing_stage = 'completed', updated_at = %s, error = NULL WHERE id = %s",
                (timestamp, document_id),
            )

    def update_document_backend(self, document_id: str, backend: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE documents SET backend = %s WHERE id = %s",
                (backend, document_id),
            )

    def fail_job(self, job_id: str, document_id: str, error: str, *, retry: bool = False) -> None:
        timestamp = now_utc()
        with self._connect() as db:
            # The ::timestamptz cast is required: when retry is False the bind
            # param is None (untyped), and PostgreSQL would otherwise infer the
            # whole CASE expression as text and reject the assignment.
            db.execute(
                """UPDATE jobs SET status = %s, stage = %s, updated_at = %s, error = %s,
                   log_excerpt = %s,
                   next_retry_at = CASE WHEN %s THEN %s::timestamptz ELSE NULL END
                   WHERE id = %s""",
                ("queued" if retry else "failed", "queued" if retry else "failed", timestamp, error[:4000], error[-12000:], retry,
                 timestamp + timedelta(hours=1) if retry else None, job_id),
            )
            db.execute(
                "UPDATE documents SET status = %s, processing_stage = %s, updated_at = %s, error = %s WHERE id = %s",
                ("queued" if retry else "failed", "queued" if retry else "failed", timestamp, error[:4000], document_id),
            )

    def get_job_log(self, job_id: str, owner_id: str, is_admin: bool = False) -> str | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT j.log_excerpt, j.error FROM jobs j
                   JOIN documents d ON d.id = j.document_id
                   WHERE j.id = %s AND d.owner_id = %s""",
                (job_id, owner_id),
            ).fetchone()
        if not row:
            return None
        return row.get("log_excerpt") or row.get("error") or "暂无失败日志"

    def requeue_job_for_retry(self, job_id: str, document_id: str) -> None:
        """Requeue a failed attempt without resetting the attempts counter."""
        timestamp = now_utc()
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET status = 'queued', stage = 'queued', updated_at = %s WHERE id = %s",
                (timestamp, job_id),
            )
            db.execute(
                "UPDATE documents SET status = 'queued', processing_stage = 'queued', updated_at = %s WHERE id = %s",
                (timestamp, document_id),
            )

    def requeue_document(self, document_id: str, owner_id: str, is_admin: bool = False) -> dict[str, Any] | None:
        timestamp = now_utc()
        with self._connect() as db:
            document = db.execute(
                "SELECT * FROM documents WHERE id = %s AND owner_id = %s",
                (document_id, owner_id),
            ).fetchone()
            if document is None:
                return None
            job = db.execute(
                "SELECT * FROM jobs WHERE document_id = %s ORDER BY created_at DESC, id DESC LIMIT 1",
                (document_id,),
            ).fetchone()
            if job is None:
                return None
            db.execute(
                "UPDATE jobs SET status = 'queued', stage = 'queued', attempts = 0, error = NULL, updated_at = %s WHERE id = %s",
                (timestamp, job["id"]),
            )
            db.execute(
                "UPDATE documents SET status = 'queued', processing_stage = 'queued', error = NULL, updated_at = %s WHERE id = %s",
                (timestamp, document_id),
            )
        return {"id": document_id, "job_id": job["id"], "status": "queued", "filename": document["filename"]}

    def replace_document_content(
        self,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        extracted: dict[str, Any],
        embeddings: list[list[float]] | None = None,
    ) -> None:
        with self._connect() as db:
            # Clear graph evidence first so reprocessing cannot leave stale
            # mentions or relations that point at deleted source chunks.
            db.execute(
                "DELETE FROM relations WHERE evidence_chunk_id IN (SELECT id FROM chunks WHERE document_id = %s)",
                (document["id"],),
            )
            db.execute(
                "DELETE FROM mentions WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id = %s)",
                (document["id"],),
            )
            db.execute("DELETE FROM chunks WHERE document_id = %s", (document["id"],))

            for chunk_index, chunk in enumerate(chunks):
                chunk_id = str(chunk.get("id") or uuid.uuid4())
                db.execute(
                    """
                    INSERT INTO chunks
                      (id, document_id, space_id, sequence, page, location, content, source_type)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        chunk_id,
                        document["id"],
                        document["space_id"],
                        chunk_index,
                        chunk.get("page"),
                        chunk.get("location"),
                        chunk["content"],
                        chunk.get("source_type", "text"),
                    ),
                )
                if embeddings and chunk_index < len(embeddings):
                    db.execute(
                        "INSERT INTO chunk_embeddings (chunk_id, vector_json) VALUES (%s, %s)",
                        (chunk_id, Jsonb(embeddings[chunk_index])),
                    )
                chunk["id"] = chunk_id

            for item in extracted.get("entities", []):
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                entity_type = str(item.get("type", "unknown"))
                row = db.execute(
                    "SELECT id FROM entities WHERE space_id = %s AND name = %s AND entity_type = %s",
                    (document["space_id"], name, entity_type),
                ).fetchone()
                entity_id = row["id"] if row else str(uuid.uuid4())
                if row is None:
                    db.execute(
                        """
                        INSERT INTO entities (id, space_id, name, entity_type, attributes_json)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (entity_id, document["space_id"], name, entity_type, Jsonb(item.get("attributes", {}))),
                    )
                if chunks:
                    chunk_index = max(0, min(len(chunks) - 1, int(item.get("chunk_index", 0))))
                    db.execute(
                        """
                        INSERT INTO mentions (id, entity_id, chunk_id, role, confidence)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            str(uuid.uuid4()),
                            entity_id,
                            chunks[chunk_index]["id"],
                            item.get("role"),
                            item.get("confidence"),
                        ),
                    )

            for relation in extracted.get("relations", []):
                subject = str(relation.get("subject", "")).strip()
                obj = str(relation.get("object", "")).strip()
                predicate = str(relation.get("predicate", "")).strip()
                if not subject or not obj or not predicate:
                    continue
                subject_row = db.execute(
                    "SELECT id FROM entities WHERE space_id = %s AND name = %s",
                    (document["space_id"], subject),
                ).fetchone()
                object_row = db.execute(
                    "SELECT id FROM entities WHERE space_id = %s AND name = %s",
                    (document["space_id"], obj),
                ).fetchone()
                if subject_row and object_row and chunks:
                    db.execute(
                        """
                        INSERT INTO relations
                          (id, space_id, subject_id, predicate, object_id, evidence_chunk_id, confidence)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            str(uuid.uuid4()),
                            document["space_id"],
                            subject_row["id"],
                            predicate,
                            object_row["id"],
                            chunks[0]["id"],
                            relation.get("confidence"),
                        ),
                    )

    @staticmethod
    def _cjk_bigrams(text: str) -> list[str]:
        """Split a query into CJK bigrams plus non-CJK words for literal scoring.

        PostgreSQL's default tokenizer cannot segment Chinese, so full-text
        search is ineffective (the whole sentence becomes one token). Scoring
        overlapping bigrams restores usable literal recall for CJK queries.
        """
        import re as _re

        segments = _re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", text)
        terms: list[str] = []
        for segment in segments:
            if not _re.fullmatch(r"[\u4e00-\u9fff]+", segment):
                terms.append(segment)
            elif len(segment) <= 2:
                terms.append(segment)
            else:
                terms.extend(segment[i : i + 2] for i in range(len(segment) - 1))
        # 去重保序
        seen: set[str] = set()
        return [t for t in terms if not (t in seen or seen.add(t))]

    def search(self, space_id: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Literal retrieval scored by CJK-bigram coverage.

        The full-text index is tried first (works for latin-script content);
        CJK queries are additionally scored by how many query bigrams each
        chunk contains, which survives where tsvector tokenization fails.
        """
        with self._connect() as db:
            full_text_rows = db.execute(
                """
                SELECT c.id, c.document_id, c.page, c.location, c.content, d.filename,
                       ts_rank_cd(c.search_vector, plainto_tsquery('simple', %s)) AS score
                FROM chunks c JOIN documents d ON d.id = c.document_id
                WHERE c.space_id = %s
                  AND c.search_vector @@ plainto_tsquery('simple', %s)
                ORDER BY score DESC
                LIMIT %s
                """,
                (query, space_id, query, limit),
            ).fetchall()

            terms = self._cjk_bigrams(query)
            if terms:
                # 每个二元组一票，按覆盖率排序；命中率过半的才算字面相关
                whens = " + ".join(
                    f"(CASE WHEN c.content LIKE %s THEN 1 ELSE 0 END)" for _ in terms
                )
                like_params = [f"%{term}%" for term in terms]
                # 占位符顺序：SELECT 打分 N 个 LIKE → 除数 → WHERE space_id → WHERE 打分 N 个 LIKE → LIMIT
                rows = db.execute(
                    f"""
                    SELECT c.id, c.document_id, c.page, c.location, c.content, d.filename,
                           ({whens})::float / %s AS score
                    FROM chunks c JOIN documents d ON d.id = c.document_id
                    WHERE c.space_id = %s AND ({whens}) > 0
                    ORDER BY score DESC
                    LIMIT %s
                    """,
                    [*like_params, float(len(terms)), space_id, *like_params, limit * 3],
                ).fetchall()
            else:
                rows = []

        merged: dict[str, dict[str, Any]] = {}
        for row in full_text_rows:
            item = dict(row)
            item["source"] = "fulltext"
            merged[item["id"]] = item
        for row in rows:
            item = dict(row)
            item["source"] = "literal"
            if item["id"] not in merged or (item.get("score") or 0) > (merged[item["id"]].get("score") or 0):
                merged[item["id"]] = item
        return list(merged.values())[:limit]

    def search_by_filename(self, space_id: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Retrieve documents whose *filename* matches the query terms.

        Filenames routinely carry information the body does not: version tags
        (``_上会版``), student or ticket ids, revision years. None of the content
        indexes can see any of it — ``search_vector`` is built from ``content``
        alone (see the ``chunks`` DDL), the bigram literal path matches
        ``content``, the embeddings embed ``content``, and entities are
        extracted from ``content``. A query naming such a token therefore
        returned nothing even when a document was named exactly that: measured
        on real data, a term present only in a filename scored 0.0 recall while
        the same term inside the body scored 1.0.

        Scoring mirrors the literal path (fraction of query terms present).
        Each matched document contributes its **opening chunk** (lowest
        ``sequence``), so the model sees what the document is rather than an
        arbitrary slice.
        """
        terms = self._cjk_bigrams(query)
        if not terms:
            return []
        lowered = [f"%{term.lower()}%" for term in terms]
        whens = " + ".join(
            "(CASE WHEN lower(doc.filename) LIKE %s THEN 1 ELSE 0 END)" for _ in terms
        )
        with self._connect() as db:
            rows = db.execute(
                f"""
                SELECT c.id, c.document_id, c.page, c.location, c.content, d.filename,
                       (s.hits)::float / %s AS score
                FROM (
                    SELECT doc.id AS document_id,
                           ({whens}) AS hits
                    FROM documents doc
                    WHERE doc.space_id = %s
                ) s
                JOIN documents d ON d.id = s.document_id
                JOIN chunks c ON c.document_id = s.document_id
                             AND c.sequence = (
                                 SELECT min(c2.sequence) FROM chunks c2
                                 WHERE c2.space_id = %s
                                   AND c2.document_id = s.document_id
                             )
                WHERE s.hits > 0
                ORDER BY score DESC, d.filename
                LIMIT %s
                """,
                # 占位符顺序：SELECT 除数 → 子查询 N 个 LIKE → 外层 space_id
                #            → 子查询 space_id → LIMIT
                [float(len(terms)), *lowered, space_id, space_id, limit],
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["source"] = "filename"
            result.append(item)
        return result

    def search_by_embedding(
        self,
        space_id: str,
        vector: list[float],
        limit: int = 20,
        min_score: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Semantic retrieval with vectorized cosine scoring.

        Scores below *min_score* are dropped so unrelated queries do not
        return noise (embedding cosine for CJK runs high; 0.30 is a practical
        relevance floor, enforced by the caller by default).
        """
        import math

        try:
            import numpy as np
        except ImportError:
            np = None

        with self._connect() as db:
            rows = db.execute(
                """
                SELECT c.id, c.document_id, c.page, c.location, c.content, d.filename, ce.vector_json
                FROM chunk_embeddings ce
                JOIN chunks c ON c.id = ce.chunk_id
                JOIN documents d ON d.id = c.document_id
                WHERE c.space_id = %s
                """,
                (space_id,),
            ).fetchall()
        if not rows:
            return []

        if np is not None and rows:
            query_vec = np.asarray(vector, dtype=np.float32)
            qnorm = np.linalg.norm(query_vec)
            if not qnorm:
                return []
            matrix = []
            keep_rows = []
            for row in rows:
                vec = row.pop("vector_json")
                if isinstance(vec, str):
                    try:
                        vec = json.loads(vec)
                    except json.JSONDecodeError:
                        continue
                arr = np.asarray(vec, dtype=np.float32)
                if arr.size != query_vec.size or not arr.any():
                    continue
                matrix.append(arr)
                keep_rows.append(row)
            if not matrix:
                return []
            scores = (np.stack(matrix) @ query_vec) / (
                np.linalg.norm(np.stack(matrix), axis=1) * qnorm + 1e-12
            )
            ranked = [
                {**row, "score": float(score)}
                for row, score in zip(keep_rows, scores)
                if score >= min_score
            ]
        else:
            def cosine(left: list[float], right: list[float]) -> float:
                if not left or len(left) != len(right):
                    return -1.0
                dot = sum(a * b for a, b in zip(left, right))
                left_norm = math.sqrt(sum(a * a for a in left))
                right_norm = math.sqrt(sum(b * b for b in right))
                return dot / (left_norm * right_norm) if left_norm and right_norm else -1.0

            ranked = []
            for row in rows:
                item = dict(row)
                vec = item.pop("vector_json")
                if isinstance(vec, str):
                    try:
                        vec = json.loads(vec)
                    except json.JSONDecodeError:
                        continue
                score = cosine(vector, vec)
                if score >= min_score:
                    ranked.append({**item, "score": score})

        ranked.sort(key=lambda item: item["score"], reverse=True)
        for item in ranked:
            item["source"] = "semantic"
        return ranked[:limit]

    def entity_context(self, space_id: str, query: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT c.id, e.id AS entity_id, e.name, e.entity_type, m.role, c.document_id, c.page,
                       c.location, c.content, d.filename
                FROM entities e
                JOIN mentions m ON m.entity_id = e.id
                JOIN chunks c ON c.id = m.chunk_id
                JOIN documents d ON d.id = c.document_id
                WHERE e.space_id = %s AND e.name ILIKE %s
                ORDER BY e.name
                LIMIT %s
                """,
                (space_id, f"%{query}%", limit),
            ).fetchall()
        results = [dict(row) for row in rows]
        for item in results:
            item["source"] = "entity"
        return results

    def relation_context(self, space_id: str, query: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT se.name AS subject, r.predicate, oe.name AS object,
                       c.document_id, c.page, d.filename
                FROM relations r
                JOIN entities se ON se.id = r.subject_id
                JOIN entities oe ON oe.id = r.object_id
                LEFT JOIN chunks c ON c.id = r.evidence_chunk_id
                LEFT JOIN documents d ON d.id = c.document_id
                WHERE r.space_id = %s AND (se.name ILIKE %s OR oe.name ILIKE %s)
                LIMIT %s
                """,
                (space_id, f"%{query}%", f"%{query}%", limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT c.*, d.filename, d.path
                FROM chunks c JOIN documents d ON d.id = c.document_id
                WHERE c.id = %s
                """,
                (evidence_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_or_create_user_space(self, owner_id: str) -> dict[str, Any]:
        """获取或创建用户的默认空间（user_{owner_id}）。"""
        space_name = f"user_{owner_id}"
        with self._connect() as db:
            row = db.execute(
                "SELECT id, name, owner_id, created_at FROM spaces WHERE owner_id = %s LIMIT 1",
                (owner_id,),
            ).fetchone()
            if row:
                return dict(row)
            space_id = str(uuid.uuid4())
            row = db.execute(
                "INSERT INTO spaces (id, name, owner_id, created_at) VALUES (%s, %s, %s, %s) RETURNING id, name, owner_id, created_at",
                (space_id, space_name, owner_id, now_utc()),
            ).fetchone()
            return dict(row)

    def list_user_documents(
        self,
        owner_id: str,
        is_admin: bool = False,
        filename_like: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        doc_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """列出该用户所有文档（跨所有空间），可选文件名模糊、时间、指定文档ID过滤。"""
        conditions: list[str] = []
        params: list[Any] = []
        # Knowledge spaces are private per account, including for admin.
        conditions.append("d.owner_id = %s")
        params.append(owner_id)
        if filename_like:
            conditions.append("d.filename ILIKE %s")
            params.append(f"%{filename_like}%")
        if created_after:
            conditions.append("d.created_at >= %s")
            params.append(created_after)
        if created_before:
            conditions.append("d.created_at <= %s")
            params.append(created_before)
        if doc_ids:
            placeholders = ",".join(["%s"] * len(doc_ids))
            conditions.append(f"d.id IN ({placeholders})")
            params.extend(doc_ids)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"""
            SELECT * FROM (
                SELECT DISTINCT ON (
                    d.owner_id,
                    d.space_id,
                    d.filename,
                    CASE WHEN d.content_hash IS NULL
                         THEN d.filename || ':' || d.size::text
                         ELSE d.content_hash END,
                    d.size
                )
                       d.id, d.owner_id, d.space_id, d.filename, d.status, d.processing_stage, d.error, d.size,
                       d.created_at, d.updated_at,
                       j.id AS job_id, j.status AS job_status, j.stage AS job_stage, j.attempts, j.error AS job_error
                FROM documents d
                LEFT JOIN LATERAL (
                    SELECT id, status, stage, attempts, error FROM jobs
                    WHERE document_id = d.id ORDER BY created_at DESC, id DESC LIMIT 1
                ) j ON TRUE
                {where}
                ORDER BY d.owner_id, d.space_id, d.filename,
                         CASE WHEN d.content_hash IS NULL
                              THEN d.filename || ':' || d.size::text
                              ELSE d.content_hash END,
                         d.size,
                         d.created_at DESC, d.id DESC
            ) deduplicated
            ORDER BY created_at DESC
        """
        with self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_user_documents_page(
        self,
        owner_id: str,
        is_admin: bool = False,
        filename_like: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        doc_ids: list[str] | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return one account-scoped, de-duplicated document page and its total."""
        conditions: list[str] = ["d.owner_id = %s"]
        params: list[Any] = [owner_id]
        if filename_like:
            conditions.append("d.filename ILIKE %s")
            params.append(f"%{filename_like}%")
        if created_after:
            conditions.append("d.created_at >= %s")
            params.append(created_after)
        if created_before:
            conditions.append("d.created_at <= %s")
            params.append(created_before)
        if doc_ids:
            placeholders = ",".join(["%s"] * len(doc_ids))
            conditions.append(f"d.id IN ({placeholders})")
            params.extend(doc_ids)
        if status:
            conditions.append("d.status = %s")
            params.append(status)

        where = " WHERE " + " AND ".join(conditions)
        deduplicated = f"""
            SELECT DISTINCT ON (
                d.owner_id,
                d.space_id,
                d.filename,
                CASE WHEN d.content_hash IS NULL
                     THEN d.filename || ':' || d.size::text
                     ELSE d.content_hash END,
                d.size
            )
                   d.id, d.owner_id, d.space_id, d.filename, d.status, d.processing_stage, d.error, d.size,
                   d.created_at, d.updated_at,
                   j.id AS job_id, j.status AS job_status, j.stage AS job_stage, j.attempts, j.error AS job_error
            FROM documents d
            LEFT JOIN LATERAL (
                SELECT id, status, stage, attempts, error FROM jobs
                WHERE document_id = d.id ORDER BY created_at DESC, id DESC LIMIT 1
            ) j ON TRUE
            {where}
            ORDER BY d.owner_id, d.space_id, d.filename,
                     CASE WHEN d.content_hash IS NULL
                          THEN d.filename || ':' || d.size::text
                          ELSE d.content_hash END,
                     d.size,
                     d.created_at DESC, d.id DESC
        """
        page = max(1, page)
        page_size = min(max(1, page_size), 100)
        offset = (page - 1) * page_size
        with self._connect() as db:
            count = db.execute(
                f"WITH deduplicated AS ({deduplicated}) SELECT COUNT(*) AS total FROM deduplicated",
                params,
            ).fetchone()
            rows = db.execute(
                f"""
                WITH deduplicated AS ({deduplicated})
                SELECT * FROM deduplicated
                ORDER BY created_at DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                [*params, page_size, offset],
            ).fetchall()
        return [dict(row) for row in rows], int(count["total"] if count else 0)

    def list_user_jobs(self, owner_id: str, is_admin: bool = False) -> list[dict[str, Any]]:
        """列出该用户所有 job 状态。"""
        conditions: list[str] = []
        params: list[Any] = []
        # Knowledge jobs follow the same strict account isolation as documents.
        conditions.append("d.owner_id = %s")
        params.append(owner_id)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"""
            SELECT j.id, j.document_id, j.status, j.attempts, j.error, j.created_at, j.updated_at,
                   d.filename
            FROM jobs j
            JOIN documents d ON d.id = j.document_id
            {where}
            ORDER BY j.created_at DESC
        """
        with self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def count_pending_jobs(self, owner_id: str) -> int:
        with self._connect() as db:
            row = db.execute(
                """SELECT COUNT(*) AS cnt FROM jobs j
                   JOIN documents d ON d.id = j.document_id
                   WHERE d.owner_id = %s AND j.status IN ('queued', 'processing')""",
                (owner_id,),
            ).fetchone()
        return int(row["cnt"] if row else 0)

    def get_document_evidence(self, document_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT c.id, c.document_id, c.page, c.location, c.content, c.source_type, d.filename
                FROM chunks c JOIN documents d ON d.id = c.document_id
                WHERE c.document_id = %s
                ORDER BY c.sequence
                """,
                (document_id,),
            ).fetchall()
        return [dict(row) for row in rows]
