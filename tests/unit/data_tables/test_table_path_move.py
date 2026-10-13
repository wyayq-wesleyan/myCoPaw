# -*- coding: utf-8 -*-
"""数据表按路径**物理迁移**（不只是记一条归属）。

归属本身不构成隔离：智能体的 ``search_path`` 由被授予的路径推导，所以一张表
如果只是「记在案件名下」而数据仍在 ``public``，就会出现两个方向都错的结果 ——
案件自己的智能体读不到它，而所有持有根的智能体仍然读得到。

因此这里验证的是「归属 = 物理位置」：

1. 归入路径后，表真的搬进该路径的 schema；
2. 移回公共区后，表真的回到 ``public``；
3. 预览等按名读表的入口能在新位置读到数据（否则用户在界面上看到 500）；
4. 未初始化的表（还没有物理表）也要能建立归属，不报错。
"""

from __future__ import annotations

import uuid

import pytest

from qwenpaw.data_tables import store as S
from qwenpaw.platform_store import get_conn, put_conn
from qwenpaw.platform_store import paths as P

from tests.unit.platform_store.test_legacy_import import (  # noqa: E402
    requires_platform_db,
)


@pytest.fixture()
def case_with_table():
    """``(owner, path_id, table_id, table_name, db_name, cleanup)``.

    Uses an existing initialized table and restores it to ``public`` afterwards, so
    a failing assertion cannot leave an account's table stranded in a path schema.
    """
    owner = "admin"
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, table_name, db_name FROM tb_hzxzh_data_table_registry "
                "WHERE owner_username = %s AND initialized = true LIMIT 1",
                (owner,),
            )
            row = cur.fetchone()
    finally:
        put_conn(conn)
    if not row:
        pytest.skip("no initialized data table available")

    table_id, table_name, db_name = str(row[0]), row[1], row[2]
    path_id = P.create_path(owner, f"移动-{uuid.uuid4().hex[:6]}")

    yield owner, path_id, table_id, table_name, db_name

    # Restore: unbind, put the table back in public, drop the path + its schema.
    try:
        P.unbind_resource(P.KIND_DATA_TABLE, table_id)
    except Exception:  # noqa: BLE001
        pass
    try:
        S.move_table_for_path(table_id, None)
    except Exception:  # noqa: BLE001
        pass
    try:
        P.delete_path(path_id)
    except Exception:  # noqa: BLE001
        pass
    try:
        S.drop_path_schema(owner, path_id)
    except Exception:  # noqa: BLE001
        pass


@requires_platform_db
def test_binding_a_table_moves_its_storage_into_the_path_schema(case_with_table):
    """归入路径 = 数据真的搬进该路径的 schema。

    这是「归属即隔离」的关键一步。只写绑定行的话，案件的智能体反而读不到自己
    的表（search_path 不含 public 之外的位置时表不在其中）。
    """
    _owner, path_id, table_id, table_name, db_name = case_with_table

    assert S.table_actual_schema(db_name, table_name) == S.ROOT_SCHEMA
    result = S.move_table_for_path(table_id, path_id)

    assert result["moved"] == "true"
    assert result["to"] == P.data_schema_for(path_id)
    assert S.table_actual_schema(db_name, table_name) == P.data_schema_for(path_id)


@requires_platform_db
def test_unbinding_returns_the_table_to_the_public_schema(case_with_table):
    """移回公共区 = 数据回到 public（未绑定即公共）。"""
    _owner, path_id, table_id, table_name, db_name = case_with_table

    S.move_table_for_path(table_id, path_id)
    assert S.table_actual_schema(db_name, table_name) == P.data_schema_for(path_id)

    result = S.move_table_for_path(table_id, None)
    assert result["to"] == S.ROOT_SCHEMA
    assert S.table_actual_schema(db_name, table_name) == S.ROOT_SCHEMA


@requires_platform_db
def test_table_data_is_intact_after_the_move(case_with_table):
    """搬动是元数据操作，数据行数不变。

    搬完之后按新位置读到的东西必须还是原来那张表 —— 否则隔离「生效」了却把
    数据弄丢，比不隔离更糟。
    """
    from qwenpaw.data_tables.connection import get_target_connection, put_target_connection

    _owner, path_id, table_id, table_name, db_name = case_with_table

    def count_rows(schema):
        conn = get_target_connection(db_name, schema)
        try:
            with conn.cursor() as cur:
                cur.execute(f'SELECT count(*) FROM "{table_name}"')
                return cur.fetchone()[0]
        finally:
            put_target_connection(conn, db_name)

    before = count_rows(S.ROOT_SCHEMA)
    S.move_table_for_path(table_id, path_id)
    after = count_rows(P.data_schema_for(path_id))

    assert after == before


@requires_platform_db
def test_move_heals_a_table_filed_before_storage_moved(case_with_table):
    """绑定与物理位置不一致时，按**实际位置**搬，而不是按归属推断。

    早期版本只写绑不定，于是存在「归属=案件、数据=public」的历史行。若按归属
    推断来源 schema，会去不存在的 schema 里 ALTER TABLE 而报错，这批数据就永远
    修不回来。
    """
    _owner, path_id, table_id, table_name, db_name = case_with_table

    # Reproduce the broken state: binding says the case, storage says public.
    P.bind_resource(path_id, P.KIND_DATA_TABLE, table_id)
    assert S.table_actual_schema(db_name, table_name) == S.ROOT_SCHEMA

    # Moving again must succeed and land it in the path schema.
    result = S.move_table_for_path(table_id, path_id)
    assert result["moved"] == "true"
    assert result["from"] == S.ROOT_SCHEMA
    assert S.table_actual_schema(db_name, table_name) == P.data_schema_for(path_id)


@requires_platform_db
def test_uninitialized_table_binds_without_error():
    """还没有物理表的表（未导入）建立归属不应报错。

    新建的数据表在导入前没有物理表；此时搬动无事可做，但归属仍要记录下来，
    导入时会直接建在正确的 schema 里。
    """
    owner = "admin"
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM tb_hzxzh_data_table_registry "
                "WHERE owner_username = %s AND initialized = false LIMIT 1",
                (owner,),
            )
            row = cur.fetchone()
    finally:
        put_conn(conn)
    if not row:
        pytest.skip("no uninitialized data table available")
    table_id = str(row[0])

    path_id = P.create_path(owner, f"未初始化-{uuid.uuid4().hex[:6]}")
    try:
        result = S.move_table_for_path(table_id, path_id)
        assert result["moved"] == "false"
        assert result["to"] == P.data_schema_for(path_id)

        # The registry's readable mirror column must move too, even when there is
        # no physical table to move. An earlier revision returned early here and
        # skipped the mirror update, leaving the raw row naming the *old* path
        # while the authoritative binding named the new one — so filing a
        # not-yet-imported table into a case looked like it had silently failed.
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT path_id FROM tb_hzxzh_data_table_registry WHERE id::text = %s",
                    (table_id,),
                )
                mirror = cur.fetchone()
        finally:
            put_conn(conn)
        assert mirror is not None
        assert str(mirror[0] or "") == path_id, (
            f"registry mirror still says {mirror[0]!r} while the binding says "
            f"{path_id!r}"
        )
    finally:
        try:
            P.unbind_resource(P.KIND_DATA_TABLE, table_id)
        except Exception:  # noqa: BLE001
            pass
        try:
            P.delete_path(path_id)
        except Exception:  # noqa: BLE001
            pass
        try:
            S.drop_path_schema(owner, path_id)
        except Exception:  # noqa: BLE001
            pass


@requires_platform_db
def test_dropping_a_path_schema_refuses_when_a_table_remains():
    """schema 里还有表时拒绝 DROP。

    路径只能在没有内容时删除，所以残留只可能是历史遗留；但一旦真有表，删除
    schema 就等于静默销毁数据 —— 宁可留下一个空 schema 让人察觉。
    """
    owner = "admin"
    path_id = P.create_path(owner, f"拒删-{uuid.uuid4().hex[:6]}")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, db_name FROM tb_hzxzh_data_table_registry "
                "WHERE owner_username = %s AND initialized = true LIMIT 1",
                (owner,),
            )
            row = cur.fetchone()
    finally:
        put_conn(conn)
    if not row:
        pytest.skip("no initialized data table available")
    table_name, db_name = row[0], row[1]

    try:
        # Put a table into the path's schema, then ask to drop that schema directly
        # (the path is not deleted here — that is the state being defended against).
        import psycopg2

        from qwenpaw.data_tables.connection import _system_db_config

        S.ensure_path_schema(db_name, P.data_schema_for(path_id))
        cfg = _system_db_config()
        raw = psycopg2.connect(
            host=cfg["host"], port=cfg["port"], dbname=db_name,
            user=cfg["username"], password=cfg["password"],
        )
        try:
            raw.autocommit = False
            with raw.cursor() as cur:
                cur.execute(
                    f'CREATE TABLE "{P.data_schema_for(path_id)}"."guard_probe" (x int)'
                )
            raw.commit()
        finally:
            raw.close()

        dropped = S.drop_path_schema(owner, path_id)
        assert dropped == []
    finally:
        # Clean the probe table and the schema.
        import psycopg2

        from qwenpaw.data_tables.connection import _system_db_config

        cfg = _system_db_config()
        raw = psycopg2.connect(
            host=cfg["host"], port=cfg["port"], dbname=db_name,
            user=cfg["username"], password=cfg["password"],
        )
        try:
            raw.autocommit = True
            with raw.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{P.data_schema_for(path_id)}" CASCADE')
        finally:
            raw.close()
        try:
            P.delete_path(path_id)
        except Exception:  # noqa: BLE001
            pass


@requires_platform_db
def test_deleting_a_table_releases_its_path_binding():
    """删表必须同时清掉归属，否则案件会永远删不掉。

    归属行的主键是 ``(resource_kind, resource_id)``，不随注册表行级联删除。留下
    的孤儿绑定本身不授权任何东西，但它让路径看起来「还有内容」，于是删案件时报
    「路径下还有内容（文档、数据表...）」，而界面上又没有任何资源可以移走 ——
    用户被困住，只能靠直接改数据库。删除表是唯一能释放它的时机。
    """
    from qwenpaw.data_tables import table_api

    owner = "admin"
    path_id = P.create_path(owner, f"删表释放-{uuid.uuid4().hex[:6]}")
    table_id = None
    try:
        created = table_api.create_table_record(
            owner,
            display_name=f"释放验证-{uuid.uuid4().hex[:6]}",
            description="删表释放归属",
            path_id=path_id,
        )
        table_id = str(created["id"])
        assert P.path_of_resource(P.KIND_DATA_TABLE, table_id) == path_id

        class _Req:
            state = type("S", (), {"user": owner})()

        table_api.delete_table(table_id, _Req())

        assert P.path_of_resource(P.KIND_DATA_TABLE, table_id) is None, (
            "table deleted but its path binding survived"
        )
        # The whole point: the path is now deletable.
        assert P.path_is_empty(path_id), "path still looks occupied after the delete"
    finally:
        if table_id:
            try:
                P.unbind_resource(P.KIND_DATA_TABLE, table_id)
            except Exception:  # noqa: BLE001
                pass
        try:
            P.delete_path(path_id)
        except Exception:  # noqa: BLE001
            pass
