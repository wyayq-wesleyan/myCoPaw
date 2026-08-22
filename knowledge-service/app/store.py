from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS spaces (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_id TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                  id TEXT PRIMARY KEY, space_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                  filename TEXT NOT NULL, path TEXT NOT NULL, media_type TEXT,
                  status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL, FOREIGN KEY(space_id) REFERENCES spaces(id)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY, document_id TEXT NOT NULL, status TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  FOREIGN KEY(document_id) REFERENCES documents(id)
                );
                CREATE TABLE IF NOT EXISTS chunks (
                  id TEXT PRIMARY KEY, document_id TEXT NOT NULL, space_id TEXT NOT NULL,
                  page INTEGER, location TEXT, content TEXT NOT NULL,
                  source_type TEXT NOT NULL DEFAULT 'text',
                  FOREIGN KEY(document_id) REFERENCES documents(id)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                  content, chunk_id UNINDEXED, document_id UNINDEXED, space_id UNINDEXED,
                  tokenize='unicode61'
                );
                CREATE TABLE IF NOT EXISTS entities (
                  id TEXT PRIMARY KEY, space_id TEXT NOT NULL, name TEXT NOT NULL,
                  entity_type TEXT, attributes_json TEXT NOT NULL DEFAULT '{}',
                  UNIQUE(space_id, name, entity_type)
                );
                CREATE TABLE IF NOT EXISTS mentions (
                  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
                  role TEXT, confidence REAL, FOREIGN KEY(entity_id) REFERENCES entities(id),
                  FOREIGN KEY(chunk_id) REFERENCES chunks(id)
                );
                CREATE TABLE IF NOT EXISTS relations (
                  id TEXT PRIMARY KEY, space_id TEXT NOT NULL, subject_id TEXT NOT NULL,
                  predicate TEXT NOT NULL, object_id TEXT NOT NULL, evidence_chunk_id TEXT,
                  confidence REAL, FOREIGN KEY(subject_id) REFERENCES entities(id),
                  FOREIGN KEY(object_id) REFERENCES entities(id)
                );
                CREATE TABLE IF NOT EXISTS chunk_embeddings (
                  chunk_id TEXT PRIMARY KEY, vector_json TEXT NOT NULL,
                  FOREIGN KEY(chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_documents_space ON documents(space_id);
                CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
                CREATE INDEX IF NOT EXISTS idx_mentions_entity ON mentions(entity_id);
                CREATE INDEX IF NOT EXISTS idx_relations_subject ON relations(subject_id);
                CREATE INDEX IF NOT EXISTS idx_relations_object ON relations(object_id);
                """
            )

    def create_space(self, name: str, owner_id: str) -> dict[str, Any]:
        space_id = str(uuid.uuid4())
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO spaces VALUES (?, ?, ?, ?)", (space_id, name, owner_id, now_iso()))
        return {"id": space_id, "name": name, "owner_id": owner_id}

    def list_spaces(self, owner_id: str, is_admin: bool = False) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM spaces WHERE owner_id = ? OR ? ORDER BY created_at",
                (owner_id, int(is_admin)),
            ).fetchall()
        return [dict(row) for row in rows]

    def space_exists(self, space_id: str, owner_id: str, is_admin: bool = False) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM spaces WHERE id = ? AND (owner_id = ? OR ?)",
                (space_id, owner_id, int(is_admin)),
            ).fetchone()
        return row is not None

    def create_document(self, space_id: str, owner_id: str, filename: str, path: str, media_type: str | None, document_id: str | None = None) -> dict[str, Any]:
        document_id = document_id or str(uuid.uuid4())
        timestamp = now_iso()
        job_id = str(uuid.uuid4())
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (document_id, space_id, owner_id, filename, path, media_type, "queued", None, timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, 0, NULL, ?, ?)",
                (job_id, document_id, "queued", timestamp, timestamp),
            )
        return {"id": document_id, "job_id": job_id, "status": "queued", "filename": filename}

    def get_document(self, document_id: str, owner_id: str, is_admin: bool = False) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM documents WHERE id = ? AND (owner_id = ? OR ?)",
                (document_id, owner_id, int(is_admin)),
            ).fetchone()
        return dict(row) if row else None

    def list_documents(
        self,
        space_id: str,
        owner_id: str,
        is_admin: bool = False,
    ) -> list[dict[str, Any]]:
        """Return documents visible in one knowledge space.

        Access is checked in the query itself so the API never leaks document
        names or processing errors across account boundaries.
        """
        with self._connect() as db:
            rows = db.execute(
                """SELECT d.*, j.id AS job_id, j.status AS job_status,
                          j.attempts AS job_attempts, j.error AS job_error
                   FROM documents d
                   LEFT JOIN jobs j ON j.id = (
                       SELECT id FROM jobs
                       WHERE document_id = d.id
                       ORDER BY created_at DESC, rowid DESC
                       LIMIT 1
                   )
                   WHERE d.space_id = ? AND (d.owner_id = ? OR ?)
                   ORDER BY d.created_at DESC""",
                (space_id, owner_id, int(is_admin)),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_job(self, job_id: str, owner_id: str, is_admin: bool = False) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT j.*, d.owner_id, d.filename, d.space_id
                   FROM jobs j JOIN documents d ON d.id = j.document_id
                   WHERE j.id = ? AND (d.owner_id = ? OR ?)""",
                (job_id, owner_id, int(is_admin)),
            ).fetchone()
        return dict(row) if row else None

    def get_document_by_id_for_worker(self, document_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        return dict(row) if row else None

    def claim_job(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            updated = db.execute(
                "UPDATE jobs SET status = 'processing', attempts = attempts + 1, updated_at = ? WHERE id = ? AND status = 'queued'",
                (now_iso(), row["id"]),
            )
            if updated.rowcount != 1:
                return None
            db.execute("UPDATE documents SET status = 'processing', updated_at = ? WHERE id = ?", (now_iso(), row["document_id"]))
        return dict(row)

    def finish_job(self, job_id: str, document_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE jobs SET status = 'completed', updated_at = ?, error = NULL WHERE id = ?", (now_iso(), job_id))
            db.execute("UPDATE documents SET status = 'completed', updated_at = ?, error = NULL WHERE id = ?", (now_iso(), document_id))

    def fail_job(self, job_id: str, document_id: str, error: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE jobs SET status = 'failed', updated_at = ?, error = ? WHERE id = ?", (now_iso(), error[:4000], job_id))
            db.execute("UPDATE documents SET status = 'failed', updated_at = ?, error = ? WHERE id = ?", (now_iso(), error[:4000], document_id))

    def requeue_document(self, document_id: str, owner_id: str, is_admin: bool = False) -> dict[str, Any] | None:
        """Queue an existing document again without duplicating its job record."""
        timestamp = now_iso()
        with self._lock, self._connect() as db:
            document = db.execute(
                "SELECT * FROM documents WHERE id = ? AND (owner_id = ? OR ?)",
                (document_id, owner_id, int(is_admin)),
            ).fetchone()
            if document is None:
                return None
            job = db.execute(
                "SELECT * FROM jobs WHERE document_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (document_id,),
            ).fetchone()
            if job is None:
                return None
            db.execute(
                "UPDATE jobs SET status = 'queued', attempts = 0, error = NULL, updated_at = ? WHERE id = ?",
                (timestamp, job["id"]),
            )
            db.execute(
                "UPDATE documents SET status = 'queued', error = NULL, updated_at = ? WHERE id = ?",
                (timestamp, document_id),
            )
        return {"id": document_id, "job_id": job["id"], "status": "queued", "filename": document["filename"]}

    def replace_document_content(self, document: dict[str, Any], chunks: list[dict[str, Any]], extracted: dict[str, Any], embeddings: list[list[float]] | None = None) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM chunks_fts WHERE document_id = ?", (document["id"],))
            db.execute("DELETE FROM mentions WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id = ?)", (document["id"],))
            db.execute("DELETE FROM chunk_embeddings WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id = ?)", (document["id"],))
            db.execute("DELETE FROM chunks WHERE document_id = ?", (document["id"],))
            for chunk_index, chunk in enumerate(chunks):
                chunk_id = chunk.get("id", str(uuid.uuid4()))
                db.execute(
                    "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (chunk_id, document["id"], document["space_id"], chunk.get("page"), chunk.get("location"), chunk["content"], chunk.get("source_type", "text")),
                )
                db.execute("INSERT INTO chunks_fts VALUES (?, ?, ?, ?)", (chunk["content"], chunk_id, document["id"], document["space_id"]))
                if embeddings and chunk_index < len(embeddings):
                    db.execute("INSERT INTO chunk_embeddings VALUES (?, ?)", (chunk_id, json.dumps(embeddings[chunk_index], separators=(",", ":"))))
                chunk["id"] = chunk_id
            for item in extracted.get("entities", []):
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                entity_type = str(item.get("type", "unknown"))
                row = db.execute("SELECT id FROM entities WHERE space_id = ? AND name = ? AND entity_type = ?", (document["space_id"], name, entity_type)).fetchone()
                entity_id = row["id"] if row else str(uuid.uuid4())
                if row is None:
                    db.execute("INSERT INTO entities VALUES (?, ?, ?, ?, ?)", (entity_id, document["space_id"], name, entity_type, json.dumps(item.get("attributes", {}), ensure_ascii=False)))
                chunk_index = max(0, min(len(chunks) - 1, int(item.get("chunk_index", 0)))) if chunks else 0
                if chunks:
                    db.execute("INSERT INTO mentions VALUES (?, ?, ?, ?, ?)", (str(uuid.uuid4()), entity_id, chunks[chunk_index]["id"], item.get("role"), item.get("confidence")))
            for relation in extracted.get("relations", []):
                subject = str(relation.get("subject", "")).strip()
                obj = str(relation.get("object", "")).strip()
                predicate = str(relation.get("predicate", "")).strip()
                if not subject or not obj or not predicate:
                    continue
                subject_row = db.execute("SELECT id FROM entities WHERE space_id = ? AND name = ?", (document["space_id"], subject)).fetchone()
                object_row = db.execute("SELECT id FROM entities WHERE space_id = ? AND name = ?", (document["space_id"], obj)).fetchone()
                if subject_row and object_row:
                    db.execute("INSERT INTO relations VALUES (?, ?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), document["space_id"], subject_row["id"], predicate, object_row["id"], chunks[0]["id"] if chunks else None, relation.get("confidence")))

    def search(self, space_id: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows: list[sqlite3.Row] = []
            # FTS5 tokenization is weak for short CJK names. Use FTS for
            # natural-language terms and always add a literal-content query
            # so a person mentioned only in a table row cannot be missed.
            try:
                rows.extend(db.execute(
                    """SELECT c.id, c.document_id, c.page, c.location, c.content, d.filename,
                              bm25(chunks_fts) AS score
                       FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.chunk_id
                       JOIN documents d ON d.id = c.document_id
                       WHERE chunks_fts MATCH ? AND c.space_id = ?
                       ORDER BY score LIMIT ?""",
                    (query, space_id, limit),
                ).fetchall())
            except sqlite3.OperationalError:
                pass
            literal_rows = db.execute(
                """SELECT c.id, c.document_id, c.page, c.location, c.content, d.filename,
                          0.0 AS score
                   FROM chunks c JOIN documents d ON d.id = c.document_id
                   WHERE c.space_id = ? AND c.content LIKE ? LIMIT ?""",
                (space_id, f"%{query}%", limit),
            ).fetchall()
        merged = {row["id"]: dict(row) for row in [*rows, *literal_rows]}
        return list(merged.values())[:limit]

    def search_by_embedding(self, space_id: str, vector: list[float], limit: int = 20) -> list[dict[str, Any]]:
        def cosine(left: list[float], right: list[float]) -> float:
            if not left or len(left) != len(right):
                return -1.0
            dot = sum(a * b for a, b in zip(left, right))
            left_norm = sum(a * a for a in left) ** 0.5
            right_norm = sum(b * b for b in right) ** 0.5
            return dot / (left_norm * right_norm) if left_norm and right_norm else -1.0

        with self._connect() as db:
            rows = db.execute(
                """SELECT c.id, c.document_id, c.page, c.location, c.content, d.filename,
                          ce.vector_json
                   FROM chunk_embeddings ce JOIN chunks c ON c.id = ce.chunk_id
                   JOIN documents d ON d.id = c.document_id
                   WHERE c.space_id = ?""",
                (space_id,),
            ).fetchall()
        ranked = []
        for row in rows:
            score = cosine(vector, json.loads(row["vector_json"]))
            item = dict(row)
            item.pop("vector_json", None)
            item["score"] = score
            ranked.append(item)
        ranked.sort(key=lambda item: item["score"], reverse=True)
        return ranked[:limit]

    def entity_context(self, space_id: str, query: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT c.id, e.id AS entity_id, e.name, e.entity_type, m.role, c.document_id, c.page,
                          c.location, c.content, d.filename
                   FROM entities e JOIN mentions m ON m.entity_id = e.id
                   JOIN chunks c ON c.id = m.chunk_id JOIN documents d ON d.id = c.document_id
                   WHERE e.space_id = ? AND e.name LIKE ? ORDER BY e.name LIMIT ?""",
                (space_id, f"%{query}%", limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def relation_context(self, space_id: str, query: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT se.name AS subject, r.predicate, oe.name AS object,
                          c.document_id, c.page, d.filename
                   FROM relations r JOIN entities se ON se.id = r.subject_id
                   JOIN entities oe ON oe.id = r.object_id
                   LEFT JOIN chunks c ON c.id = r.evidence_chunk_id
                   LEFT JOIN documents d ON d.id = c.document_id
                   WHERE r.space_id = ? AND (se.name LIKE ? OR oe.name LIKE ?)
                   LIMIT ?""",
                (space_id, f"%{query}%", f"%{query}%", limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT c.*, d.filename, d.path FROM chunks c JOIN documents d ON d.id = c.document_id WHERE c.id = ?", (evidence_id,)).fetchone()
        return dict(row) if row else None

    def get_document_evidence(self, document_id: str) -> list[dict[str, Any]]:
        """获取指定文档的所有evidence片段"""
        with self._connect() as db:
            rows = db.execute(
                """SELECT c.id, c.document_id, c.page, c.location, c.content, c.source_type,
                          d.filename
                   FROM chunks c JOIN documents d ON d.id = c.document_id
                   WHERE c.document_id = ?
                   ORDER BY CASE WHEN c.page IS NULL THEN 0 ELSE 1 END, c.page, c.rowid""",
                (document_id,),
            ).fetchall()
        return [dict(row) for row in rows]
