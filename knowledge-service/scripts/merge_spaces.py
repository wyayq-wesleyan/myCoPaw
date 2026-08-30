#!/usr/bin/env python3
"""把每个用户所有历史空间文档合并到该用户的默认空间。"""
from __future__ import annotations

import argparse
import uuid
from datetime import datetime, timezone

import psycopg


DATABASE_URL = "postgresql://huizhi_knowledge:huizhi_knowledge@host.docker.internal:15432/huizhi_knowledge"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def merge_spaces(dry_run: bool = False) -> None:
    """合并每个用户的所有空间到默认空间 user_{owner_id}。"""
    conn = psycopg.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        # 找到所有有文档的用户
        cur.execute("SELECT DISTINCT owner_id FROM documents")
        owners = [r[0] for r in cur.fetchall()]
        print(f"Found {len(owners)} owners with documents")

        for owner_id in owners:
            print(f"\nProcessing owner: {owner_id}")

            # 获取或创建默认空间
            default_name = f"user_{owner_id}"
            cur.execute(
                "SELECT id FROM spaces WHERE owner_id = %s AND name = %s",
                (owner_id, default_name),
            )
            row = cur.fetchone()
            if row:
                default_space_id = row[0]
                print(f"  Found existing default space: {default_space_id}")
            else:
                default_space_id = str(uuid.uuid4())
                print(f"  Creating default space: {default_space_id}")
                if not dry_run:
                    cur.execute(
                        "INSERT INTO spaces (id, name, owner_id, created_at) VALUES (%s, %s, %s, %s)",
                        (default_space_id, default_name, owner_id, now_utc()),
                    )

            # 找到该用户所有其他空间
            cur.execute(
                "SELECT id, name FROM spaces WHERE owner_id = %s AND id != %s",
                (owner_id, default_space_id),
            )
            other_spaces = cur.fetchall()

            if not other_spaces:
                print(f"  No other spaces to merge")
                continue

            other_space_ids = [s[0] for s in other_spaces]
            other_space_names = [s[1] for s in other_spaces]
            print(f"  Found {len(other_spaces)} other spaces: {other_space_names}")

            # 更新 documents
            placeholders = ",".join(["%s"] * len(other_space_ids))
            cur.execute(
                f"UPDATE documents SET space_id = %s WHERE space_id IN ({placeholders})",
                [default_space_id] + other_space_ids,
            )
            docs_updated = cur.rowcount
            print(f"  Updated {docs_updated} documents")

            # 更新 chunks
            cur.execute(
                f"UPDATE chunks SET space_id = %s WHERE space_id IN ({placeholders})",
                [default_space_id] + other_space_ids,
            )
            chunks_updated = cur.rowcount
            print(f"  Updated {chunks_updated} chunks")

            # 更新 entities
            cur.execute(
                f"UPDATE entities SET space_id = %s WHERE space_id IN ({placeholders})",
                [default_space_id] + other_space_ids,
            )
            entities_updated = cur.rowcount
            print(f"  Updated {entities_updated} entities")

            # 更新 relations
            cur.execute(
                f"UPDATE relations SET space_id = %s WHERE space_id IN ({placeholders})",
                [default_space_id] + other_space_ids,
            )
            relations_updated = cur.rowcount
            print(f"  Updated {relations_updated} relations")

            # 删除旧空间
            if not dry_run:
                cur.execute(
                    f"DELETE FROM spaces WHERE id IN ({placeholders})",
                    other_space_ids,
                )
                print(f"  Deleted {len(other_spaces)} old spaces")
            else:
                print(f"  [DRY RUN] Would delete {len(other_spaces)} old spaces")

        if not dry_run:
            conn.commit()
            print("\nMigration committed successfully")
        else:
            print("\n[DRY RUN] No changes committed")

    except Exception as e:
        print(f"\nError during migration: {e}")
        conn.rollback()
        print("Changes rolled back")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="合并每个用户的历史空间到默认空间")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅显示将要执行的操作，不实际修改数据库",
    )
    args = parser.parse_args()
    merge_spaces(dry_run=args.dry_run)
