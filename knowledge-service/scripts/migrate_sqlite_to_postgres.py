"""One-time migration utility for pre-PostgreSQL knowledge-service metadata."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from psycopg.types.json import Jsonb

from app.store import Store


TABLES = (
    "spaces",
    "documents",
    "jobs",
    "chunks",
    "entities",
    "mentions",
    "relations",
    "chunk_embeddings",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate knowledge-service SQLite metadata to PostgreSQL")
    parser.add_argument("sqlite_path", type=Path, help="Path to legacy metadata.sqlite3")
    parser.add_argument("--database-url", required=True, help="PostgreSQL connection URL")
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="Delete existing PostgreSQL knowledge metadata before importing",
    )
    return parser.parse_args()


def json_value(value: str | None) -> Jsonb:
    try:
        return Jsonb(json.loads(value or "{}"))
    except json.JSONDecodeError:
        return Jsonb({})


def ensure_empty(store: Store, truncate: bool) -> None:
    with store._connect() as db:  # noqa: SLF001 - migration needs one atomic import transaction.
        count = db.execute("SELECT COUNT(*) AS count FROM documents").fetchone()["count"]
        if count and not truncate:
            raise RuntimeError(
                "The PostgreSQL knowledge database is not empty. Re-run with --truncate only after backup."
            )
        if truncate:
            db.execute(
                """
                TRUNCATE TABLE chunk_embeddings, relations, mentions, entities, chunks, jobs, documents, spaces
                """
            )


def migrate(sqlite_path: Path, database_url: str, truncate: bool) -> None:
    if not sqlite_path.is_file():
        raise FileNotFoundError(f"Legacy SQLite database not found: {sqlite_path}")

    store = Store(database_url)
    ensure_empty(store, truncate)

    source = sqlite3.connect(sqlite_path)
    source.row_factory = sqlite3.Row
    counts: dict[str, int] = defaultdict(int)
    sequences: dict[str, int] = defaultdict(int)

    with store._connect() as target:  # noqa: SLF001 - keep the import atomic.
        for row in source.execute("SELECT * FROM spaces"):
            target.execute(
                "INSERT INTO spaces (id, name, owner_id, created_at) VALUES (%s, %s, %s, %s)",
                (row["id"], row["name"], row["owner_id"], row["created_at"]),
            )
            counts["spaces"] += 1

        for row in source.execute("SELECT * FROM documents"):
            target.execute(
                """
                INSERT INTO documents
                  (id, space_id, owner_id, filename, path, media_type, status, error, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                tuple(row[key] for key in (
                    "id", "space_id", "owner_id", "filename", "path", "media_type",
                    "status", "error", "created_at", "updated_at",
                )),
            )
            counts["documents"] += 1

        for row in source.execute("SELECT * FROM jobs"):
            target.execute(
                """
                INSERT INTO jobs (id, document_id, status, attempts, error, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                tuple(row[key] for key in (
                    "id", "document_id", "status", "attempts", "error", "created_at", "updated_at",
                )),
            )
            counts["jobs"] += 1

        for row in source.execute("SELECT rowid AS source_rowid, * FROM chunks ORDER BY document_id, source_rowid"):
            sequence = sequences[row["document_id"]]
            sequences[row["document_id"]] += 1
            target.execute(
                """
                INSERT INTO chunks
                  (id, document_id, space_id, sequence, page, location, content, source_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    row["id"], row["document_id"], row["space_id"], sequence, row["page"],
                    row["location"], row["content"], row["source_type"],
                ),
            )
            counts["chunks"] += 1

        for row in source.execute("SELECT * FROM entities"):
            target.execute(
                """
                INSERT INTO entities (id, space_id, name, entity_type, attributes_json)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    row["id"], row["space_id"], row["name"], row["entity_type"] or "unknown",
                    json_value(row["attributes_json"]),
                ),
            )
            counts["entities"] += 1

        for row in source.execute("SELECT * FROM mentions"):
            target.execute(
                "INSERT INTO mentions (id, entity_id, chunk_id, role, confidence) VALUES (%s, %s, %s, %s, %s)",
                (row["id"], row["entity_id"], row["chunk_id"], row["role"], row["confidence"]),
            )
            counts["mentions"] += 1

        for row in source.execute("SELECT * FROM relations"):
            target.execute(
                """
                INSERT INTO relations
                  (id, space_id, subject_id, predicate, object_id, evidence_chunk_id, confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    row["id"], row["space_id"], row["subject_id"], row["predicate"], row["object_id"],
                    row["evidence_chunk_id"], row["confidence"],
                ),
            )
            counts["relations"] += 1

        for row in source.execute("SELECT * FROM chunk_embeddings"):
            target.execute(
                "INSERT INTO chunk_embeddings (chunk_id, vector_json) VALUES (%s, %s)",
                (row["chunk_id"], json_value(row["vector_json"])),
            )
            counts["chunk_embeddings"] += 1

    source.close()
    print("SQLite -> PostgreSQL migration complete")
    for table in TABLES:
        print(f"{table}: {counts[table]}")


if __name__ == "__main__":
    args = parse_args()
    migrate(args.sqlite_path, args.database_url, args.truncate)
