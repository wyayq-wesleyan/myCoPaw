from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


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
                kwargs={"row_factory": dict_row},
                name="knowledge-store-pool",
            )
        except ImportError:
            # psycopg_pool 未安装时退回直连模式（开发环境可用）
            pass
        self._init_db()

    def _connect(self):
        """Return a connection context manager; pooled when available."""
        if self._pool is not None:
            return self._pool.connection()
        return psycopg.connect(self.database_url, row_factory=dict_row)

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
              created_at TIMESTAMPTZ NOT NULL,
              updated_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY,
              document_id TEXT NOT NULL REFERENCES documents(id),
              status TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0,
              error TEXT,
              created_at TIMESTAMPTZ NOT NULL,
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
                "SELECT * FROM spaces WHERE owner_id = %s OR %s ORDER BY created_at",
                (owner_id, is_admin),
            ).fetchall()
        return [dict(row) for row in rows]

    def space_exists(self, space_id: str, owner_id: str, is_admin: bool = False) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM spaces WHERE id = %s AND (owner_id = %s OR %s)",
                (space_id, owner_id, is_admin),
            ).fetchone()
        return row is not None

    def create_document(
        self,
        space_id: str,
        owner_id: str,
        filename: str,
        path: str,
        media_type: str | None,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        document_id = document_id or str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        timestamp = now_utc()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO documents
                  (id, space_id, owner_id, filename, path, media_type, status, error, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'queued', NULL, %s, %s)
                """,
                (document_id, space_id, owner_id, filename, path, media_type, timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO jobs (id, document_id, status, attempts, error, created_at, updated_at)
                VALUES (%s, %s, 'queued', 0, NULL, %s, %s)
                """,
                (job_id, document_id, timestamp, timestamp),
            )
        return {"id": document_id, "job_id": job_id, "status": "queued", "filename": filename}

    def get_document(self, document_id: str, owner_id: str, is_admin: bool = False) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM documents WHERE id = %s AND (owner_id = %s OR %s)",
                (document_id, owner_id, is_admin),
            ).fetchone()
        return dict(row) if row else None

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
                WHERE d.space_id = %s AND (d.owner_id = %s OR %s)
                ORDER BY d.created_at DESC
                """,
                (space_id, owner_id, is_admin),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_job(self, job_id: str, owner_id: str, is_admin: bool = False) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT j.*, d.owner_id, d.filename, d.space_id
                FROM jobs j JOIN documents d ON d.id = j.document_id
                WHERE j.id = %s AND (d.owner_id = %s OR %s)
                """,
                (job_id, owner_id, is_admin),
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
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE jobs SET status = 'processing', attempts = attempts + 1, updated_at = %s WHERE id = %s",
                (timestamp, row["id"]),
            )
            db.execute(
                "UPDATE documents SET status = 'processing', updated_at = %s WHERE id = %s",
                (timestamp, row["document_id"]),
            )
        return dict(row)

    def finish_job(self, job_id: str, document_id: str) -> None:
        timestamp = now_utc()
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET status = 'completed', updated_at = %s, error = NULL WHERE id = %s",
                (timestamp, job_id),
            )
            db.execute(
                "UPDATE documents SET status = 'completed', updated_at = %s, error = NULL WHERE id = %s",
                (timestamp, document_id),
            )

    def fail_job(self, job_id: str, document_id: str, error: str) -> None:
        timestamp = now_utc()
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET status = 'failed', updated_at = %s, error = %s WHERE id = %s",
                (timestamp, error[:4000], job_id),
            )
            db.execute(
                "UPDATE documents SET status = 'failed', updated_at = %s, error = %s WHERE id = %s",
                (timestamp, error[:4000], document_id),
            )

    def requeue_job_for_retry(self, job_id: str, document_id: str) -> None:
        """Requeue a failed attempt without resetting the attempts counter."""
        timestamp = now_utc()
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET status = 'queued', updated_at = %s WHERE id = %s",
                (timestamp, job_id),
            )
            db.execute(
                "UPDATE documents SET status = 'queued', updated_at = %s WHERE id = %s",
                (timestamp, document_id),
            )

    def requeue_document(self, document_id: str, owner_id: str, is_admin: bool = False) -> dict[str, Any] | None:
        timestamp = now_utc()
        with self._connect() as db:
            document = db.execute(
                "SELECT * FROM documents WHERE id = %s AND (owner_id = %s OR %s)",
                (document_id, owner_id, is_admin),
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
                "UPDATE jobs SET status = 'queued', attempts = 0, error = NULL, updated_at = %s WHERE id = %s",
                (timestamp, job["id"]),
            )
            db.execute(
                "UPDATE documents SET status = 'queued', error = NULL, updated_at = %s WHERE id = %s",
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
        for row in [*rows, *full_text_rows]:
            item = dict(row)
            if item["id"] not in merged or (item.get("score") or 0) > (merged[item["id"]].get("score") or 0):
                merged[item["id"]] = item
        return list(merged.values())[:limit]

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
        return [dict(row) for row in rows]

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
        if not is_admin:
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
            SELECT d.id, d.filename, d.status, d.error, d.created_at, d.updated_at,
                   j.id AS job_id, j.status AS job_status, j.attempts, j.error AS job_error
            FROM documents d
            LEFT JOIN LATERAL (
                SELECT id, status, attempts, error FROM jobs
                WHERE document_id = d.id ORDER BY created_at DESC, id DESC LIMIT 1
            ) j ON TRUE
            {where}
            ORDER BY d.created_at DESC
        """
        with self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_user_jobs(self, owner_id: str, is_admin: bool = False) -> list[dict[str, Any]]:
        """列出该用户所有 job 状态。"""
        conditions: list[str] = []
        params: list[Any] = []
        if not is_admin:
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
