from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

try:  # the pool is optional: without it ``_connect`` falls back to direct links
    from psycopg_pool import PoolTimeout
except ImportError:  # pragma: no cover - knowledge image always ships it
    class PoolTimeout(Exception):  # type: ignore[no-redef]
        """Stand-in so ``_connect``'s except clause stays importable."""

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


def _vector_literal(vector: Any) -> str:
    """Render an embedding as pgvector's ``[a,b,c]`` text form.

    Passed as a parameter and cast with ``%s::vector`` rather than formatted into
    SQL: the values come from an embedding service, so they must never be treated
    as SQL. ``repr``-style shortest float formatting keeps the literal small; the
    precision is well beyond what float32 storage can distinguish.
    """
    return "[" + ",".join(f"{float(x):.8g}" for x in vector) + "]"


class Store:
    """PostgreSQL-backed durable store for parsed knowledge and retrieval data."""

    # Sized against the *thread* ceiling, not a user count. Every store call
    # reaches this pool through ``asyncio.to_thread``, i.e. from one of the
    # server's worker threads (anyio defaults to 40). A pool smaller than that
    # ceiling means concurrent requests queue on connections and, at the limit,
    # fail with an opaque PoolTimeout — which is what "连接已满" was.
    #
    # Budget: this pool + the graph pool are per process, and there are two
    # processes (api, worker). See ``POOL_WAIT_SECONDS`` for the wait policy and
    # the compose ``max_connections`` for the PostgreSQL-side ceiling.
    POOL_MIN_SIZE = 2
    POOL_MAX_SIZE = 30

    # How long a caller waits for a free connection before giving up. The
    # psycopg_pool default is 30s, which is long enough to look like a hang; this
    # keeps a saturated pool a bounded delay and makes the eventual error
    # name the cause instead of surfacing as a bare timeout.
    POOL_WAIT_SECONDS = 20.0

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
                timeout=self.POOL_WAIT_SECONDS,
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
        """Return a connection context manager; pooled when available.

        A pool that stays saturated past ``POOL_WAIT_SECONDS`` raises
        ``PoolTimeout`` from deep inside psycopg_pool, which reads as an opaque
        internal failure in the UI. Re-raising it with the pool's own numbers
        makes the cause and the remedy obvious from the message alone.
        """
        if self._pool is None:
            return LoggingConnection.connect(
                self.database_url, row_factory=dict_row,
            )
        try:
            return self._pool.connection()
        except PoolTimeout as exc:  # pragma: no cover - needs a saturated pool
            raise RuntimeError(
                "知识服务数据库连接池已满："
                f"{self.POOL_WAIT_SECONDS:.0f} 秒内未取得连接"
                f"（池上限 {self.POOL_MAX_SIZE}）。"
                "请检查是否有长时间未结束的查询，或调大 "
                "KNOWLEDGE 连接池上限并同步放宽 PostgreSQL max_connections。",
            ) from exc

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
            # Which path a document's **graph data** belongs to, i.e. its LightRAG
            # workspace. The path model itself lives in the main app (which does not
            # share a database with this service), so the main app supplies the
            # workspace on upload and this table records it. Kept here rather than
            # reusing ``space_id`` because the graph is per path, not per space: two
            # cases can share a space but must not share a graph.
            """
            CREATE TABLE IF NOT EXISTS document_workspaces (
              document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
              workspace TEXT NOT NULL,
              updated_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_document_workspaces_workspace
              ON document_workspaces (workspace)
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
            # Every statement here runs in its own savepoint. A failed statement
            # aborts the surrounding PostgreSQL transaction, and an aborted
            # transaction poisons *everything* after it -- including statements
            # that are themselves wrapped in a savepoint, so the isolation below
            # only holds while no bare statement runs. Two outages came from
            # missing that: jobs.stage stayed missing after an upgrade while the
            # job queries kept failing, and a database without the vector
            # extension killed this process at import time.
            self._migrate(db, statements)
            # A database created by an older release predates some columns.
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
                    # Which worker instance owns a processing job. Recovery
                    # checks the **owner's** heartbeat freshness rather than
                    # matching heartbeat rows to job ids: a heartbeat row is
                    # keyed by worker and updated every ~10s regardless of
                    # which job it names, so "some worker heartbeated recently"
                    # must never be confused with "this job's owner is alive".
                    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS owner_worker_id TEXT",
                ],
            )
            # ── pgvector 向量列 ──────────────────────────────────────────────
            # 原实现把整个空间的所有向量读回 Python 再逐个算余弦，开销与内存都
            # 随语料线性增长（实测 6 万 chunk = 34.6s / 3.7GB，15 万 chunk 直接
            # OOM）。改为数据库内近邻检索后，延迟与内存与语料规模基本无关。
            #
            # 分三步、各自容错：老库先加列（此刻为空，很快），回填由启动后的
            # 后台任务做，回填完再建 HNSW 索引。任何一步失败都只降级到原来的
            # jsonb 全量比对，不影响可用性。
            self._migrate(db, ["CREATE EXTENSION IF NOT EXISTS vector"])
            if not self._vector_type_available(db):
                # A database without pgvector is a supported (degraded) install,
                # not a broken one: retrieval falls back to the in-Python jsonb
                # path. Say so loudly, because the operator otherwise only sees
                # semantic search being slow, with nothing to explain why.
                logger.warning(
                    "pgvector: the 'vector' extension is unavailable on this "
                    "database, so semantic search stays on the jsonb fallback. "
                    "Install it to enable the vector path "
                    "(see knowledge-service/docker-compose.postgres.yml, which "
                    "uses the pgvector/pgvector:pg17 image for this reason)."
                )
                return
            dimensions = self._resolve_embedding_dimensions(db)
            if dimensions:
                self._migrate(
                    db,
                    [
                        f"ALTER TABLE chunk_embeddings "
                        f"ADD COLUMN IF NOT EXISTS embedding vector({dimensions})",
                    ],
                )
                # The HNSW index is **never** built on this path, for two reasons.
                #
                # Maintaining the index during the backfill costs a graph insert per
                # row: measured 50 rows/s with it versus 2900 rows/s without — 58x
                # slower, i.e. an hour versus a minute for a 150k-row corpus.
                #
                # And this code runs inside ``Store.__init__``, which ``main.py``
                # executes at *import* time — before uvicorn can answer anything.
                # Building the index here made start-up block for as long as the
                # build takes (measured 22s at 50k rows; minutes at production
                # size), so the console's health probe timed out and the knowledge
                # page showed 503 until it finished. The index is created by the
                # worker's idle branch (``rebuild_vector_index_if_needed``) instead,
                # where taking minutes blocks nothing.
                if self._column_is_vector_typed(db, dimensions):
                    remaining = self._count_unconverted(db, dimensions)
                    if remaining:
                        logger.info(
                            "pgvector: %d embedding(s) still on the jsonb fallback; "
                            "the background backfill will convert them and then "
                            "build the HNSW index",
                            remaining,
                        )

    # ------------------------------------------------------------------
    # pgvector migration helpers
    # ------------------------------------------------------------------

    # Dimension used when the environment does not pin one and the database has no
    # rows to measure. Matches the shipped embedding model (Qwen3-Embedding).
    DEFAULT_EMBEDDING_DIMENSIONS = 1024

    # HNSW parameters: m=16 / ef_construction=64 are pgvector's defaults, which are
    # the documented starting point for a few-hundred-thousand-row corpus.
    HNSW_INDEX_NAME = "idx_chunk_embeddings_hnsw"

    @classmethod
    def _create_hnsw_index_statement(cls) -> str:
        return (
            f"CREATE INDEX IF NOT EXISTS {cls.HNSW_INDEX_NAME} "
            "ON chunk_embeddings USING hnsw (embedding vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 64)"
        )

    @classmethod
    def _count_unconverted(cls, db: Any, dimensions: int) -> int:
        """Rows not yet copied into the vector column (wrong-width rows excluded)."""
        try:
            row = db.execute(
                """
                SELECT count(*) AS n FROM chunk_embeddings
                WHERE embedding IS NULL
                  AND jsonb_typeof(vector_json) = 'array'
                  AND jsonb_array_length(vector_json) = %s
                """,
                (dimensions,),
            ).fetchone()
            return int((row or {}).get("n") or 0)
        except Exception:  # noqa: BLE001
            return 0

    @staticmethod
    def _vector_type_available(db: Any) -> bool:
        """Whether the ``vector`` type exists, i.e. pgvector is installed.

        Asked with ``to_regtype`` rather than by attempting a cast. A cast that
        fails raises ``undefined_object``, and an error raised without a
        savepoint around it marks the **whole** transaction aborted — after
        which every later statement fails, including the ones ``_migrate`` wraps
        in savepoints, and ``_init_db`` dies on commit. ``main.py`` builds this
        store at import time, so that killed the API process on any database
        without pgvector instead of degrading to the jsonb path.

        The savepoint here is belt-and-braces: ``to_regtype`` returns NULL for an
        unknown type rather than raising, but the guard costs nothing and keeps
        the invariant "no bare statement runs in this transaction" true.
        """
        try:
            with db.transaction():
                row = db.execute(
                    "SELECT to_regtype('vector') IS NOT NULL AS ok",
                ).fetchone()
            return bool(row and row.get("ok"))
        except Exception as exc:  # noqa: BLE001 - unreachable DB is "not yet"
            logger.warning("pgvector: capability probe failed: %s", exc)
            return False

    @classmethod
    def _resolve_embedding_dimensions(cls, db: Any) -> int | None:
        """Width of the ``vector`` column, or ``None`` to skip the pgvector path.

        Resolution order: an explicit ``EMBEDDING_DIMENSIONS`` wins (the operator
        knows what their endpoint returns); otherwise the width is measured from a
        row already in the table, so an existing install migrates to the dimension
        it actually stored; otherwise the model default. A measured value that
        contradicts the configured one is logged and the configured value is kept —
        the column must not change shape mid-life or the index is invalidated.
        """
        configured = os.getenv("EMBEDDING_DIMENSIONS", "").strip()
        configured_n = int(configured) if configured.isdigit() and int(configured) > 0 else None

        measured: int | None = None
        try:
            # Savepoint: this probe casts through the ``vector`` type, so on a
            # database that has the extension listed but not usable it raises.
            # Unwrapped, that error would poison the caller's transaction.
            with db.transaction():
                row = db.execute(
                    "SELECT vector_dims((vector_json::text)::vector) AS dims "
                    "FROM chunk_embeddings "
                    "WHERE jsonb_typeof(vector_json) = 'array' "
                    "  AND jsonb_array_length(vector_json) BETWEEN 1 AND 8192 "
                    "LIMIT 1",
                ).fetchone()
            if row and row.get("dims"):
                measured = int(row["dims"])
        except Exception as exc:  # noqa: BLE001 - extension or data shape missing
            # WARNING, not DEBUG: failing to measure means the column width comes
            # from a default or an env var, and a wrong width silently disables
            # the whole vector path. At the production ``info`` level a debug
            # line here is invisible.
            logger.warning("pgvector: could not measure existing dimensions: %s", exc)

        if configured_n and measured and configured_n != measured:
            logger.warning(
                "pgvector: EMBEDDING_DIMENSIONS=%s but stored vectors are %s-wide; "
                "using the configured value — re-embed documents if this is wrong",
                configured_n, measured,
            )
        return configured_n or measured or cls.DEFAULT_EMBEDDING_DIMENSIONS

    @staticmethod
    def _column_is_vector_typed(db: Any, dimensions: int) -> bool:
        """True when ``chunk_embeddings.embedding`` is ``vector(dimensions)``.

        Guards the case where the column exists with a *different* width (the
        embedding model changed): building an index on it would either fail or
        index nothing useful, and the jsonb fallback is still correct.
        """
        try:
            row = db.execute(
                "SELECT format_type(a.atttypid, a.atttypmod) AS t "
                "FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "WHERE c.relname = 'chunk_embeddings' "
                "  AND a.attname = 'embedding' AND a.attnum > 0",
            ).fetchone()
        except Exception:  # noqa: BLE001
            return False
        if not row:
            return False
        declared = str(row.get("t") or "")
        if declared == f"vector({dimensions})":
            return True
        logger.warning(
            "pgvector: existing embedding column is %s, expected vector(%d); "
            "leaving it alone (jsonb fallback stays in use)",
            declared, dimensions,
        )
        return False

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
                       j.attempts AS job_attempts, j.error AS job_error,
                       j.next_retry_at AS job_next_retry_at
                FROM documents d
                LEFT JOIN LATERAL (
                    SELECT id, status, attempts, error, next_retry_at FROM jobs
                    WHERE document_id = d.id ORDER BY created_at DESC, id DESC LIMIT 1
                ) j ON TRUE
                WHERE d.space_id = %s
                ORDER BY d.created_at DESC
                """,
                (space_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_all_spaces_internal(self) -> list[dict[str, Any]]:
        """List every space across all owners, for trusted maintenance tasks.

        Startup maintenance (the LightRAG naming migration) runs with no request
        context, so it has no owner to scope by — the same reason
        :meth:`list_documents_internal` exists. Never expose this over HTTP:
        unlike the scoped ``list_spaces``, it is not filtered by owner.
        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM spaces ORDER BY created_at",
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
                       j.attempts AS job_attempts, j.error AS job_error,
                       j.next_retry_at AS job_next_retry_at
                FROM documents d
                LEFT JOIN LATERAL (
                    SELECT id, status, attempts, error, next_retry_at
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

    def claim_job(
        self,
        worker_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Claim one queued job safely across multiple worker processes.

        The owner and a fresh heartbeat are written **in the same transaction**
        as the claim. Without that, a claim that committed before its owner
        managed to write a heartbeat looks unowned to a concurrent recovery
        pass, which would then steal — and duplicate — the job.
        """
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
                "UPDATE jobs SET status = 'processing', stage = 'parsing', attempts = attempts + 1, updated_at = %s, owner_worker_id = %s WHERE id = %s",
                (timestamp, worker_id, row["id"]),
            )
            db.execute(
                "UPDATE documents SET status = 'processing', processing_stage = 'parsing', started_at = COALESCE(started_at, %s), updated_at = %s WHERE id = %s",
                (timestamp, timestamp, row["document_id"]),
            )
            if worker_id:
                db.execute(
                    """INSERT INTO worker_heartbeats (worker_id, status, job_id, stage, updated_at)
                       VALUES (%s, 'processing', %s, 'parsing', %s)
                       ON CONFLICT (worker_id) DO UPDATE SET
                         status = EXCLUDED.status,
                         job_id = EXCLUDED.job_id,
                         stage = EXCLUDED.stage,
                         updated_at = EXCLUDED.updated_at""",
                    (worker_id, row["id"], timestamp),
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

    # A job is treated as abandoned only when its owner has been silent for
    # longer than this. Workers heartbeat every ~10s, so 60s tolerates several
    # missed ticks (GC pause, slow local DB) without ever stranding a job.
    RECOVERY_HEARTBEAT_GRACE_SECONDS = 60

    # Heartbeat rows whose worker has been silent for a full day are deleted:
    # every restart leaves one row behind (the id is per instance), so without
    # a cap the table only ever grows.
    HEARTBEAT_RETENTION_SECONDS = 86400

    def recover_interrupted_jobs(
        self,
        grace_seconds: int | None = None,
        reclaim_owner: str | None = None,
    ) -> int:
        """Requeue jobs left in ``processing`` by a worker that stopped.

        Ownership is decided per **worker**, not per job row. Every claim
        records ``jobs.owner_worker_id`` and the owner keeps its heartbeat row
        fresh every ~10s (updated unconditionally by the heartbeat thread, not
        only on stage changes), so "abandoned" means exactly one of:

        * the owner's heartbeat row is stale (older than the grace window) —
          the worker holding the job is dead, no matter which job its row
          names;
        * the row has no recorded owner at all — written by a pre-upgrade
          worker; fall back to the claim's own age plus "no fresh heartbeat
          names this job" for those;
        * ``reclaim_owner`` matches — the caller is the **same worker instance
          restarting**. A worker only runs while it holds a session-level
          advisory lock on its id, so two live processes cannot share one id;
          when this instance boots, every job owned by its id provably has no
          live owner and is reclaimed immediately, without waiting out the
          grace window (the pre-upgrade behaviour for a fast container
          restart).

        The condition must never be "some worker heartbeated recently": that
        would make a restart leave a dead worker's job unrecoverable for as
        long as the *surviving* workers keep heartbeating — up to the slow
        ``requeue_stale_jobs`` threshold, i.e. an hour.

        The idle loop's periodic sweep calls this without ``reclaim_owner``:
        a worker that died without a restart (OOM kill, crash loop) has its
        jobs reclaimed once the grace window passes, on the next idle tick,
        instead of waiting for the hour-scale stale sweep.
        """
        grace = (
            self.RECOVERY_HEARTBEAT_GRACE_SECONDS
            if grace_seconds is None
            else max(0, int(grace_seconds))
        )
        timestamp = now_utc()
        cutoff = timestamp - timedelta(seconds=grace)
        retention_cutoff = timestamp - timedelta(
            seconds=self.HEARTBEAT_RETENTION_SECONDS,
        )
        with self._connect() as db:
            requeued = db.execute(
                """
                UPDATE jobs SET status = 'queued', stage = 'queued', updated_at = %s,
                    next_retry_at = NULL,
                    error = COALESCE(error, '任务因服务重启中断，已自动恢复')
                WHERE status = 'processing'
                  AND (
                        (owner_worker_id IS NOT NULL AND NOT EXISTS (
                            SELECT 1 FROM worker_heartbeats h
                            WHERE h.worker_id = jobs.owner_worker_id
                              AND h.updated_at > %s
                        ))
                     OR (owner_worker_id IS NULL
                         AND jobs.updated_at < %s
                         AND NOT EXISTS (
                             SELECT 1 FROM worker_heartbeats h
                             WHERE h.job_id = jobs.id
                               AND h.updated_at > %s
                         ))
                     OR (%s::text IS NOT NULL AND owner_worker_id = %s)
                  )
                RETURNING id, document_id
                """,
                (timestamp, cutoff, cutoff, cutoff, reclaim_owner, reclaim_owner),
            ).fetchall()
            # Reset only the documents whose own job was actually reclaimed:
            # a document still owned by a live worker must keep its
            # "processing" stage, or the UI would show it as queued while it
            # is in fact running.
            for row in requeued:
                db.execute(
                    """UPDATE documents SET status = 'queued',
                           processing_stage = 'queued', updated_at = %s
                       WHERE id = %s""",
                    (timestamp, row["document_id"]),
                )
            db.execute(
                "DELETE FROM worker_heartbeats WHERE updated_at < %s",
                (retention_cutoff,),
            )
        return len(requeued)

    def requeue_stale_jobs(self, max_age_seconds: int, max_attempts: int) -> int:
        """Reclaim jobs whose processing has gone silent for too long.

        Start-up recovery only handles the case where the worker itself
        restarted. A job that hangs *inside* a run (a parser that never
        returns, a worker process that died while the container stayed up) is
        never reclaimed: the worker is single-threaded, so everything queued
        behind it sits at "等待 Worker" forever while the LLM stays idle.
        Sweeping on a timer converts that permanent stall into a retry.

        ``max_age_seconds`` must comfortably exceed the longest legitimate
        single stage (the parser timeout). Reclaiming a healthy-but-slow run
        would yank it out from under the worker and duplicate the work, so the
        caller derives the threshold from the configured parser timeout.

        Jobs that already used up their attempts are failed instead of
        requeued, so a document that reliably hangs cannot spin forever.
        """
        timestamp = now_utc()
        cutoff = timestamp - timedelta(seconds=max(60, int(max_age_seconds)))
        note = "任务长时间无进展，已自动重新排队"
        give_up_note = "任务长时间无进展，且已用尽重试次数"
        with self._connect() as db:
            # Exhausted first: the requeue below must not pick these up again.
            # ``error`` is overwritten rather than coalesced — the previous
            # value is the "requeued" notice, which would misreport a job that
            # has now been given up on.
            exhausted = db.execute(
                """
                UPDATE jobs SET status = 'failed', stage = 'failed', updated_at = %s,
                    next_retry_at = NULL,
                    error = %s,
                    log_excerpt = COALESCE(log_excerpt, %s)
                WHERE status = 'processing' AND updated_at < %s AND attempts >= %s
                RETURNING id, document_id
                """,
                (timestamp, give_up_note, give_up_note, cutoff, max_attempts),
            ).fetchall()
            for row in exhausted:
                db.execute(
                    """UPDATE documents SET status = 'failed', processing_stage = 'failed',
                       updated_at = %s, error = %s WHERE id = %s""",
                    (timestamp, give_up_note, row["document_id"]),
                )

            requeued = db.execute(
                """
                UPDATE jobs SET status = 'queued', stage = 'queued', updated_at = %s,
                    next_retry_at = NULL,
                    error = COALESCE(error, %s)
                WHERE status = 'processing' AND updated_at < %s
                RETURNING id, document_id
                """,
                (timestamp, note, cutoff),
            ).fetchall()
            for row in requeued:
                db.execute(
                    """UPDATE documents SET status = 'queued', processing_stage = 'queued',
                       updated_at = %s WHERE id = %s""",
                    (timestamp, row["document_id"]),
                )
        return len(exhausted) + len(requeued)

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

    # Rows per backfill round trip. Large enough to keep the job short, small
    # enough that a run never holds a meaningful amount of memory.
    VECTOR_BACKFILL_BATCH = 500

    def backfill_embedding_vectors(self, max_batches: int = 20) -> int:
        """Copy ``vector_json`` into the pgvector ``embedding`` column, in batches.

        Existing installs have every vector in the jsonb column only. The column
        has to be filled before the HNSW index can serve queries, and doing it at
        startup would block the first request for minutes on a large corpus, so it
        runs in the background and is resumable.

        **One statement per batch, converting in the database.** The conversion is
        ``(vector_json::text)::vector`` executed by PostgreSQL, so the 20 KB vector
        text never crosses the wire — neither read into Python nor written back.
        That is not just tidier: passing the literal back in means an extra full
        copy of every vector through the client, and the earlier round-trip version
        measured ~40 rows/s (about an hour for a 150k-row corpus).

        Idempotent and self-limiting: each call converts up to
        ``max_batches × VECTOR_BACKFILL_BATCH`` rows and returns how many were
        converted, so the caller can interleave it with real work and simply stop
        when it returns 0. Rows already converted are skipped by the
        ``embedding IS NULL`` predicate, so a restart mid-way resumes rather than
        repeating.

        The scan requires the stored array to be exactly the column's width. That
        is what makes the loop safe to repeat on a batch that cannot progress: a
        row whose width disagrees (the embedding model was swapped) is never
        selected again, so one bad generation of vectors cannot stall the rest.
        Those rows keep working through the jsonb fallback.

        Returns the number of rows converted.
        """
        if not self._vector_column_available():
            return 0

        dimensions = self._vector_column_dimensions()
        if not dimensions:
            return 0

        converted = 0
        for _ in range(max(1, int(max_batches))):
            try:
                with self._connect() as db:
                    result = db.execute(
                        """
                        WITH batch AS (
                            SELECT chunk_id, vector_json
                            FROM chunk_embeddings
                            WHERE embedding IS NULL
                              AND jsonb_typeof(vector_json) = 'array'
                              AND jsonb_array_length(vector_json) = %s
                            LIMIT %s
                        )
                        UPDATE chunk_embeddings AS ce
                        SET embedding = (b.vector_json::text)::vector
                        FROM batch b
                        WHERE ce.chunk_id = b.chunk_id
                          AND ce.embedding IS NULL
                        """,
                        (dimensions, self.VECTOR_BACKFILL_BATCH),
                    )
                    batch_done = result.rowcount or 0
            except Exception as exc:  # noqa: BLE001 - keep the service usable
                logger.warning(
                    "pgvector backfill: batch failed (%s); "
                    "remaining rows stay on the jsonb fallback",
                    exc,
                )
                break

            converted += batch_done
            if batch_done == 0:
                # Either nothing is left, or the remaining rows are all
                # wrong-width. Another round would select the same rows.
                break

        return converted

    def _vector_column_dimensions(self) -> int | None:
        """Width declared on ``chunk_embeddings.embedding``, or ``None``."""
        try:
            with self._connect() as db:
                row = db.execute(
                    "SELECT format_type(a.atttypid, a.atttypmod) AS t "
                    "FROM pg_attribute a "
                    "JOIN pg_class c ON c.oid = a.attrelid "
                    "WHERE c.relname = 'chunk_embeddings' "
                    "  AND a.attname = 'embedding' AND a.attnum > 0",
                ).fetchone()
        except Exception:  # noqa: BLE001
            return None
        declared = str((row or {}).get("t") or "")
        if declared.startswith("vector(") and declared.endswith(")"):
            inner = declared[len("vector("):-1]
            return int(inner) if inner.isdigit() else None
        return None

    def backfill_embedding_vectors_until_done(
        self, max_total_batches: int = 100_000,
    ) -> int:
        """Run ``backfill_embedding_vectors`` until the column needs no more work.

        Convenience for the worker's idle branch, which wants the whole job done
        rather than one batch per idle tick (the queue can stay empty for hours).
        Bounded so a pathological database cannot trap the worker here.
        """
        total = 0
        for _ in range(max_total_batches):
            done = self.backfill_embedding_vectors(max_batches=1)
            if done == 0:
                break
            total += done
        if total:
            self.rebuild_vector_index_if_needed()
        return total

    @staticmethod
    def _space_has_chunks(db: Any, space_id: str) -> bool:
        """True when the space contains at least one chunk (cheap, bounded to 1)."""
        try:
            row = db.execute(
                "SELECT 1 AS ok FROM chunks WHERE space_id = %s LIMIT 1",
                (space_id,),
            ).fetchone()
            return row is not None
        except Exception:  # noqa: BLE001
            return False

    def count_unconverted_embeddings(self) -> int:
        """Rows still waiting for their pgvector value; ``-1`` when unknown.

        ``-1`` rather than ``0`` on failure: the callers use this to decide whether
        the column is finished, and reporting "0 remaining" for a database that
        merely could not be reached would let them conclude the migration is done
        and stop retrying. ``0`` means "confirmed nothing left".
        """
        if not self._vector_column_available():
            return 0
        dimensions = self._vector_column_dimensions()
        if not dimensions:
            return 0
        try:
            with self._connect() as db:
                row = db.execute(
                    """
                    SELECT count(*) AS n FROM chunk_embeddings
                    WHERE embedding IS NULL
                      AND jsonb_typeof(vector_json) = 'array'
                      AND jsonb_array_length(vector_json) = %s
                    """,
                    (dimensions,),
                ).fetchone()
            return int((row or {}).get("n") or 0)
        except Exception:  # noqa: BLE001 - unknown, not zero
            logger.warning(
                "pgvector: could not count unconverted embeddings",
                exc_info=True,
            )
            return -1

    def rebuild_vector_index_if_needed(self) -> bool:
        """Create the HNSW index once every convertible row has a vector.

        Deliberately deferred until the backfill is done: building the graph
        incrementally during the conversion costs 58x (measured 50 vs 2900 rows/s),
        so the index is created in one pass over a finished column instead.

        A no-op when the index already exists or rows are still pending, and
        best-effort overall — without it queries still work through the column
        scan (and, for any remaining rows, the jsonb fallback), just slower.
        """
        if not self._vector_column_available():
            return False
        dimensions = self._vector_column_dimensions()
        if not dimensions:
            return False
        try:
            with self._connect() as db:
                if self._count_unconverted(db, dimensions):
                    return False
                existing = db.execute(
                    "SELECT 1 AS ok FROM pg_indexes WHERE indexname = %s",
                    (self.HNSW_INDEX_NAME,),
                ).fetchone()
                if existing:
                    return True
            # Autocommit connection rather than the pooled session: building an
            # HNSW index over a large table is a long single statement and must not
            # sit inside the pool's implicit transaction.
            with psycopg.connect(
                self.database_url, autocommit=True, connect_timeout=30,
            ) as db:
                # Left at the server default. Raising maintenance_work_mem makes the
                # build faster, but it is charged against shared memory
                # (``/dev/shm``), which is small in the default container and fails
                # the whole statement with "could not resize shared memory segment
                # ... No space left on device". The default is slower but always
                # fits.
                db.execute("SET statement_timeout = 0")
                db.execute(self._create_hnsw_index_statement())
            logger.info("pgvector: HNSW index created after backfill")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("pgvector: index creation skipped (%s)", exc)
            return False

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

            # Resolved once per save rather than per chunk: the pgvector column may
            # be absent (extension unavailable), in which case the jsonb column is
            # the only store and retrieval uses the fallback scan.
            vector_ready = self._vector_column_available()

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
                    embedding = embeddings[chunk_index]
                    if vector_ready:
                        # Write the pgvector column alongside the jsonb copy, so new
                        # documents are searchable through the index immediately and
                        # never depend on the backfill job. The jsonb column stays
                        # the durable source of truth and the fallback for a
                        # database where the extension could not be created.
                        db.execute(
                            "INSERT INTO chunk_embeddings "
                            "(chunk_id, vector_json, embedding) "
                            "VALUES (%s, %s, %s::vector)",
                            (chunk_id, Jsonb(embedding), _vector_literal(embedding)),
                        )
                    else:
                        db.execute(
                            "INSERT INTO chunk_embeddings (chunk_id, vector_json) "
                            "VALUES (%s, %s)",
                            (chunk_id, Jsonb(embedding)),
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
        """Semantic retrieval, scored in the database.

        ``pgvector`` with an HNSW index does the neighbour search, so both the
        latency and the memory are independent of how much material the account
        has uploaded. The previous implementation pulled every vector in the space
        into Python and scored them there: measured at 0.52 ms and ~62 MB per
        10k chunks, i.e. 34.6 s / 3.7 GB at 60k chunks and an OOM kill around
        150k — which is exactly the "2000 documents and chat got slow" report.

        Falls back to the old in-Python scan when the ``embedding`` column is
        absent or empty (a database mid-migration, or a deployment where the
        extension could not be created), so retrieval never breaks — it only
        loses the speed-up until the backfill catches up.

        Scores below *min_score* are dropped so unrelated queries do not
        return noise (embedding cosine for CJK runs high; 0.30 is a practical
        relevance floor, enforced by the caller by default).
        """
        if not vector:
            return []

        with self._connect() as db:
            if self._has_vector_column(db):
                rows = self._vector_search_sql(db, space_id, vector, limit, min_score)
                if rows:
                    # The query deliberately fetches more candidates than *limit*
                    # so that discounting scores below *min_score* still leaves
                    # enough rows — the same contract as the in-Python path below,
                    # which filters and truncates rather than returning everything.
                    ranked = sorted(
                        (r for r in rows if (r.get("score") or 0.0) >= min_score),
                        key=lambda item: item["score"],
                        reverse=True,
                    )
                    for item in ranked:
                        item["source"] = "semantic"
                    return ranked[:limit]
                # Empty from the index is ambiguous: it can mean "no close match",
                # which is a normal answer, or "this space is not backfilled yet".
                # Only the second case may take the full-scan fallback — and the
                # distinction must be made by looking at the data, **not** at the
                # row count. Treating "no match" as "not migrated" would scan every
                # vector in the account, which is what made a large account OOM.
                if self._space_is_backfilled(db, space_id):
                    # A backfilled space with content should normally produce
                    # candidates. Getting none hints at a stale HNSW index (built
                    # before a bulk load); ``REINDEX INDEX
                    # idx_chunk_embeddings_hnsw`` is the remedy. Logged rather than
                    # silently returning [] so the cause is diagnosable.
                    if self._space_has_chunks(db, space_id):
                        logger.warning(
                            "pgvector: no neighbours returned for a populated, "
                            "fully-backfilled space (%s); if this repeats, "
                            "REINDEX INDEX %s",
                            space_id, self.HNSW_INDEX_NAME,
                        )
                    return []
            rows = self._load_all_vectors(db, space_id)

        return self._score_in_python(rows, vector, limit, min_score)

    @staticmethod
    def _space_is_backfilled(db: Any, space_id: str) -> bool:
        """True when every chunk in the space has its pgvector value.

        Used to decide whether an empty index result is authoritative. Bounded to
        one row so the check stays cheap on a large space.
        """
        try:
            row = db.execute(
                """
                SELECT 1 AS pending FROM chunk_embeddings ce
                JOIN chunks c ON c.id = ce.chunk_id
                WHERE c.space_id = %s AND ce.embedding IS NULL
                LIMIT 1
                """,
                (space_id,),
            ).fetchone()
            return row is None
        except Exception:  # noqa: BLE001 - unknown means "fall back", the safe side
            return False

    @staticmethod
    def _has_vector_column(db: Any) -> bool:
        """True once the pgvector column exists (even if not yet backfilled)."""
        try:
            row = db.execute(
                "SELECT 1 AS ok FROM information_schema.columns "
                "WHERE table_name = 'chunk_embeddings' AND column_name = 'embedding'",
            ).fetchone()
            return bool(row)
        except Exception:  # noqa: BLE001
            return False

    def _vector_column_available(self) -> bool:
        """Whether ``chunk_embeddings.embedding`` can be written to.

        **Only a positive answer is cached.** The column is permanent once it
        exists, so ``True`` is safe to remember for the process lifetime — but
        ``False`` is not: it is also what an unreachable database returns, and the
        ``Store`` is a process-wide singleton. Caching that made a single
        transient error (a hiccup during the first probe, or the window before
        ``CREATE EXTENSION`` finished) disable pgvector for good: writes fell back
        to jsonb, the backfill converted nothing, the HNSW index was never built,
        and semantic search stayed on the slow in-Python path until a restart. On
        failure the next call re-probes instead.
        """
        cached = getattr(self, "_vector_col_cache", None)
        if cached:
            return True
        try:
            with self._connect() as db:
                available = self._has_vector_column(db)
        except Exception:  # noqa: BLE001 - unreachable DB is "not yet", not "never"
            logger.warning(
                "pgvector: could not check for the embedding column; will retry",
                exc_info=True,
            )
            return False
        if available:
            self._vector_col_cache = True
        return available

    def _vector_search_sql(
        self,
        db: Any,
        space_id: str,
        vector: list[float],
        limit: int,
        min_score: float,
    ) -> list[dict[str, Any]]:
        """Nearest neighbours via the HNSW index.

        ``<=>`` is cosine *distance*; ``1 - distance`` is the cosine similarity the
        caller expects. Asking for more rows than ``limit`` keeps the previous
        behaviour of cutting off by ``min_score`` after ranking rather than
        silently returning fewer than asked.

        Rows whose ``embedding`` is still NULL (not yet backfilled) are skipped by
        the index scan itself; the caller falls back to the full scan only when
        this returns nothing at all, so a partially backfilled space still returns
        the vectors that are ready.
        """
        literal = "[" + ",".join(f"{float(x):.8g}" for x in vector) + "]"
        candidates = max(limit * 4, limit + 20)
        try:
            return db.execute(
                """
                SELECT c.id, c.document_id, c.page, c.location, c.content, d.filename,
                       1 - (ce.embedding <=> %s::vector) AS score
                FROM chunk_embeddings ce
                JOIN chunks c ON c.id = ce.chunk_id
                JOIN documents d ON d.id = c.document_id
                WHERE c.space_id = %s AND ce.embedding IS NOT NULL
                ORDER BY ce.embedding <=> %s::vector
                LIMIT %s
                """,
                (literal, space_id, literal, candidates),
            ).fetchall()
        except Exception as exc:  # noqa: BLE001 - fall back rather than fail
            logger.warning(
                "pgvector: neighbour search failed, falling back to full scan: %s",
                exc,
            )
            return []

    @staticmethod
    def _load_all_vectors(db: Any, space_id: str) -> list[dict[str, Any]]:
        """Load every vector in the space — the pre-pgvector fallback path."""
        try:
            return db.execute(
                """
                SELECT c.id, c.document_id, c.page, c.location, c.content, d.filename,
                       ce.vector_json
                FROM chunk_embeddings ce
                JOIN chunks c ON c.id = ce.chunk_id
                JOIN documents d ON d.id = c.document_id
                WHERE c.space_id = %s
                """,
                (space_id,),
            ).fetchall()
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _score_in_python(
        rows: list[dict[str, Any]],
        vector: list[float],
        limit: int,
        min_score: float,
    ) -> list[dict[str, Any]]:
        """Cosine-score ``rows`` in Python (fallback for un-backfilled data)."""
        import math

        try:
            import numpy as np
        except ImportError:
            np = None

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
                vec = row.pop("vector_json", None)
                if vec is None:
                    continue
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

    def set_document_workspace(self, document_id: str, workspace: str) -> None:
        """Record which workspace a document's graph data belongs to."""
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO document_workspaces (document_id, workspace, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (document_id)
                DO UPDATE SET workspace = EXCLUDED.workspace,
                              updated_at = EXCLUDED.updated_at
                """,
                (document_id, workspace, now_utc()),
            )

    def get_document_workspace(self, document_id: str) -> str | None:
        """The workspace a document belongs to, or ``None`` if unrecorded."""
        with self._connect() as db:
            row = db.execute(
                "SELECT workspace FROM document_workspaces WHERE document_id = %s",
                (document_id,),
            ).fetchone()
        return str(row["workspace"]) if row else None

    def list_pending_document_ids(self, owner_id: str) -> list[str]:
        """该账号仍在 queued/processing 的文档 id。

        界面要把「处理中」标在**具体哪条路径**上，需要的不是总数而是哪些文档在跑
        —— 主应用把每条路径的可见文档集合与这份列表求交集，即可得到该路径自己的
        计数。与 ``count_pending_jobs`` 同源（同样的状态集合），避免两处口径不一致。
        """
        with self._connect() as db:
            rows = db.execute(
                """SELECT DISTINCT d.id FROM jobs j
                   JOIN documents d ON d.id = j.document_id
                   WHERE d.owner_id = %s AND j.status IN ('queued', 'processing')""",
                (owner_id,),
            ).fetchall()
        return [str(row["id"]) for row in rows]

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
