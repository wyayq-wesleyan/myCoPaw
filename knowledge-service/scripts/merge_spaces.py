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
        cur.execute("SELECT DISTINCT owner_id FROM documents")
        owners = [r[0] for r in cur.fetchall()]
        print(f"Found {len(owners)} owners with documents")

        for owner_id in owners:
            print(f"\nProcessing owner: {owner_id}")

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

            placeholders = ",".join(["%s"] * len(other_space_ids))

            # 更新 documents
            cur.execute(
                f"UPDATE documents SET space_id = %s WHERE space_id IN ({placeholders})",
                [default_space_id] + other_space_ids,
            )
            print(f"  Updated {cur.rowcount} documents")

            # 更新 chunks
            cur.execute(
                f"UPDATE chunks SET space_id = %s WHERE space_id IN ({placeholders})",
                [default_space_id] + other_space_ids,
            )
            print(f"  Updated {cur.rowcount} chunks")

            # 先清理 mentions（引用了即将删除的实体的）
            cur.execute(f"""
                DELETE FROM mentions
                WHERE entity_id IN (
                    SELECT e1.id FROM entities e1
                    JOIN entities e2 ON e1.name = e2.name AND e1.entity_type = e2.entity_type
                    WHERE e1.id > e2.id
                    AND e1.space_id = e2.space_id
                    AND e1.space_id IN ({placeholders})
                )
            """, other_space_ids)
            print(f"  Cleaned {cur.rowcount} mentions for intra-space dup entities")

            # 先在每个旧空间内部去重
            cur.execute(
                f"""
                DELETE FROM entities e1
                USING entities e2
                WHERE e1.id > e2.id
                AND e1.space_id = e2.space_id
                AND e1.name = e2.name
                AND e1.entity_type = e2.entity_type
                AND e1.space_id IN ({placeholders})
                """,
                other_space_ids,
            )
            print(f"  Deleted {cur.rowcount} intra-space duplicate entities")

            # 清理跨旧空间重复实体的 mentions
            cur.execute(f"""
                DELETE FROM mentions
                WHERE entity_id IN (
                    SELECT e1.id FROM entities e1
                    JOIN entities e2 ON e1.name = e2.name AND e1.entity_type = e2.entity_type
                    WHERE e1.id > e2.id
                    AND e1.space_id != e2.space_id
                    AND e1.space_id IN ({placeholders})
                    AND e2.space_id IN ({placeholders})
                )
            """, other_space_ids + other_space_ids)
            print(f"  Cleaned {cur.rowcount} mentions for cross-space dup entities")

            # 跨旧空间去重：保留一个，删除其余
            cur.execute(
                f"""
                DELETE FROM entities e1
                USING entities e2
                WHERE e1.id > e2.id
                AND e1.space_id != e2.space_id
                AND e1.name = e2.name
                AND e1.entity_type = e2.entity_type
                AND e1.space_id IN ({placeholders})
                AND e2.space_id IN ({placeholders})
                """,
                other_space_ids + other_space_ids,
            )
            print(f"  Deleted {cur.rowcount} cross-old-space duplicate entities")

            # 迁移剩余 entities
            cur.execute(
                f"UPDATE entities SET space_id = %s WHERE space_id IN ({placeholders})",
                [default_space_id] + other_space_ids,
            )
            print(f"  Updated {cur.rowcount} entities")

            # 清理 mentions（引用了已删除实体的）
            cur.execute("DELETE FROM mentions WHERE entity_id NOT IN (SELECT id FROM entities)")
            print(f"  Cleaned {cur.rowcount} orphaned mentions")

            # 清理 relations（引用了已删除实体的）
            cur.execute("""
                DELETE FROM relations
                WHERE subject_id NOT IN (SELECT id FROM entities)
                   OR object_id NOT IN (SELECT id FROM entities)
            """)
            print(f"  Cleaned {cur.rowcount} orphaned relations")

            # 更新 relations
            cur.execute(
                f"UPDATE relations SET space_id = %s WHERE space_id IN ({placeholders})",
                [default_space_id] + other_space_ids,
            )
            print(f"  Updated {cur.rowcount} relations")

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
