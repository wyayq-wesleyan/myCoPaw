#!/usr/bin/env python3
"""
pytest_data_tables.py - 数据分析模块自动化测试脚本

测试环境: 本地 PostgreSQL (huizhi_knowledge @ host.docker.internal:15432)
运行方式: python3 -m pytest pytest_data_tables.py -v

前置条件:
1. 知识服务容器 huizhi-knowledge-api 运行中
2. 已配置瀚高数据源（或使用本地 PG17 模拟）
3. 已生成测试文件（运行 generate_test_data.py）
"""

import json
import os
import sys
import time
import uuid

import psycopg2
import psycopg2.extras
import pytest

# 测试文件目录
TEST_DIR = os.path.join(os.path.dirname(__file__), "test_files")

# 数据库连接配置
METADATA_DB = {
    "host": os.environ.get("TEST_PG_HOST", "localhost"),
    "port": int(os.environ.get("TEST_PG_PORT", "15432")),
    "dbname": "huizhi_knowledge",
    "user": "huizhi_knowledge",
    "password": "huizhi_knowledge",
}


@pytest.fixture(scope="module")
def metadata_conn():
    """获取元数据数据库连接"""
    conn = psycopg2.connect(**METADATA_DB)
    conn.autocommit = True
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def cleanup(metadata_conn):
    """测试后清理"""
    yield
    with metadata_conn.cursor() as cur:
        # 清理测试创建的表
        cur.execute("DELETE FROM data_import_job WHERE table_id IN (SELECT id FROM data_table_registry WHERE table_name LIKE 'tb_test_%')")
        cur.execute("DELETE FROM data_table_registry WHERE table_name LIKE 'tb_test_%'")
        metadata_conn.commit()
    print("\n[CLEANUP] Test data cleaned up")


class TestMetadataSchema:
    """测试元数据表结构"""

    def test_data_source_config_exists(self, metadata_conn):
        with metadata_conn.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'data_source_config'
                ORDER BY ordinal_position
            """)
            cols = [r[0] for r in cur.fetchall()]
            assert "id" in cols
            assert "name" in cols
            assert "host" in cols
            assert "port" in cols
            assert "username" in cols
            assert "password_encrypted" in cols

    def test_data_table_registry_exists(self, metadata_conn):
        with metadata_conn.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'data_table_registry'
                ORDER BY ordinal_position
            """)
            cols = [r[0] for r in cur.fetchall()]
            assert "id" in cols
            assert "owner_username" in cols
            assert "table_name" in cols
            assert "description" in cols
            assert "columns" in cols
            assert "row_count" in cols
            assert "initialized" in cols

    def test_data_import_job_exists(self, metadata_conn):
        with metadata_conn.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'data_import_job'
                ORDER BY ordinal_position
            """)
            cols = [r[0] for r in cur.fetchall()]
            assert "id" in cols
            assert "table_id" in cols
            assert "filename" in cols
            assert "status" in cols
            assert "total_rows" in cols
            assert "success_rows" in cols
            assert "failed_rows" in cols
            assert "errors" in cols


class TestTableCRUD:
    """测试数据表 CRUD 操作"""

    def test_create_table(self, metadata_conn):
        table_name = f"tb_test_{uuid.uuid4().hex[:8]}"
        with metadata_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO data_table_registry (owner_username, db_name, table_name, description, columns)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    "admin",
                    "",
                    table_name,
                    "测试数据表",
                    json.dumps([
                        {"comment": "订单编号", "column_name": None, "type": None, "nullable": True, "is_primary_key": False},
                        {"comment": "客户名称", "column_name": None, "type": None, "nullable": True, "is_primary_key": False},
                    ]),
                ),
            )
            row_id = cur.fetchone()[0]
            metadata_conn.commit()
            assert row_id is not None

            # 验证创建成功
            cur.execute("SELECT * FROM data_table_registry WHERE id = %s", (row_id,))
            row = cur.fetchone()
            assert row is not None
            # Column order: id, owner_username, db_name, table_name, description, ...
            assert row[1] == "admin"  # owner_username
            assert row[3] == table_name  # table_name
            assert row[4] == "测试数据表"  # description

    def test_list_tables(self, metadata_conn):
        with metadata_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) as cnt FROM data_table_registry WHERE owner_username = 'admin'")
            total = cur.fetchone()["cnt"]
            assert total >= 0

    def test_delete_table(self, metadata_conn):
        table_name = f"tb_test_{uuid.uuid4().hex[:8]}"
        with metadata_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO data_table_registry (owner_username, db_name, table_name, description, columns)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                ("admin", "", table_name, "待删除表", json.dumps([])),
            )
            row_id = cur.fetchone()[0]
            metadata_conn.commit()

            # 删除
            cur.execute("DELETE FROM data_table_registry WHERE id = %s", (row_id,))
            metadata_conn.commit()

            # 验证已删除
            cur.execute("SELECT 1 FROM data_table_registry WHERE id = %s", (row_id,))
            assert cur.fetchone() is None


class TestImportJobTracking:
    """测试导入任务跟踪"""

    def test_create_import_job(self, metadata_conn):
        # 先创建一个表
        table_name = f"tb_test_{uuid.uuid4().hex[:8]}"
        with metadata_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO data_table_registry (owner_username, db_name, table_name, description, columns)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                ("admin", "", table_name, "导入测试表", json.dumps([])),
            )
            table_id = cur.fetchone()[0]
            metadata_conn.commit()

            # 创建导入任务
            job_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO data_import_job (id, table_id, filename, status)
                VALUES (%s, %s, %s, 'pending')
                """,
                (job_id, table_id, "test.csv"),
            )
            metadata_conn.commit()

            # 验证
            cur.execute("SELECT * FROM data_import_job WHERE id = %s", (job_id,))
            row = cur.fetchone()
            assert row is not None
            assert row[3] == "pending"  # status

            # 清理
            cur.execute("DELETE FROM data_import_job WHERE id = %s", (job_id,))
            cur.execute("DELETE FROM data_table_registry WHERE id = %s", (table_id,))
            metadata_conn.commit()

    def test_job_status_flow(self, metadata_conn):
        table_name = f"tb_test_{uuid.uuid4().hex[:8]}"
        with metadata_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO data_table_registry (owner_username, db_name, table_name, description, columns)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                ("admin", "", table_name, "状态测试表", json.dumps([])),
            )
            table_id = cur.fetchone()[0]
            metadata_conn.commit()

            job_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO data_import_job (id, table_id, filename, status) VALUES (%s, %s, %s, 'pending')",
                (job_id, table_id, "test.csv"),
            )
            metadata_conn.commit()

            # 模拟状态流转: pending -> parsing -> loading -> completed
            for status in ["parsing", "loading", "completed"]:
                cur.execute("UPDATE data_import_job SET status=%s WHERE id=%s", (status, job_id))
                metadata_conn.commit()
                cur.execute("SELECT status FROM data_import_job WHERE id=%s", (job_id,))
                assert cur.fetchone()[0] == status

            # 清理
            cur.execute("DELETE FROM data_import_job WHERE id = %s", (job_id,))
            cur.execute("DELETE FROM data_table_registry WHERE id = %s", (table_id,))
            metadata_conn.commit()


class TestDataIntegrity:
    """测试数据完整性约束"""

    def test_unique_table_name_per_user(self, metadata_conn):
        table_name = f"tb_test_{uuid.uuid4().hex[:8]}"
        with metadata_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO data_table_registry (owner_username, db_name, table_name, description, columns)
                VALUES (%s, %s, %s, %s, %s)
                """,
                ("admin", "", table_name, "唯一性测试", json.dumps([])),
            )
            metadata_conn.commit()

            # 尝试创建同名表
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute(
                    """
                    INSERT INTO data_table_registry (owner_username, db_name, table_name, description, columns)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    ("admin", "", table_name, "重复", json.dumps([])),
                )
                metadata_conn.commit()

            # 清理
            cur.execute("DELETE FROM data_table_registry WHERE table_name = %s", (table_name,))
            metadata_conn.commit()

    def test_cascade_delete(self, metadata_conn):
        table_name = f"tb_test_{uuid.uuid4().hex[:8]}"
        with metadata_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO data_table_registry (owner_username, db_name, table_name, description, columns)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                ("admin", "", table_name, "级联删除测试", json.dumps([])),
            )
            table_id = cur.fetchone()[0]
            metadata_conn.commit()

            # 创建关联的导入任务
            job_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO data_import_job (id, table_id, filename, status) VALUES (%s, %s, %s, 'pending')",
                (job_id, table_id, "test.csv"),
            )
            metadata_conn.commit()

            # 删除表（应级联删除任务）
            cur.execute("DELETE FROM data_table_registry WHERE id = %s", (table_id,))
            metadata_conn.commit()

            # 验证任务也被删除
            cur.execute("SELECT 1 FROM data_import_job WHERE id = %s", (job_id,))
            assert cur.fetchone() is None


class TestSearchFiltering:
    """测试搜索过滤功能"""

    def test_keyword_search(self, metadata_conn):
        # 创建测试数据
        names = []
        with metadata_conn.cursor() as cur:
            for suffix in ["alpha", "beta", "gamma"]:
                tname = f"tb_test_search_{suffix}"
                cur.execute(
                    """
                    INSERT INTO data_table_registry (owner_username, db_name, table_name, description, columns)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    ("admin", "", tname, f"搜索测试 {suffix} 描述", json.dumps([])),
                )
                names.append(tname)
            metadata_conn.commit()

            # 关键词搜索
            cur.execute(
                "SELECT table_name FROM data_table_registry WHERE owner_username='admin' AND table_name ILIKE %s",
                ("%search%",),
            )
            found = [r[0] for r in cur.fetchall()]
            assert len(found) >= 3

            # 清理
            for n in names:
                cur.execute("DELETE FROM data_table_registry WHERE table_name = %s", (n,))
            metadata_conn.commit()

    def test_date_range_filter(self, metadata_conn):
        with metadata_conn.cursor() as cur:
            # 查询指定时间范围内的表
            cur.execute(
                """
                SELECT COUNT(*) FROM data_table_registry
                WHERE owner_username = 'admin'
                AND created_at >= %s AND created_at <= %s
                """,
                ("2024-01-01", "2024-12-31"),
            )
            count = cur.fetchone()[0]
            assert count >= 0  # 至少不报错


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
