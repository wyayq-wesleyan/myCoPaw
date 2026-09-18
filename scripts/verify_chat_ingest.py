"""End-to-end check: chat-driven create_table -> import -> row count.

Runs against the real local PostgreSQL, exercising the actual tool functions
with only the request context faked (the same way a chat turn would provide it).
Creates its own scratch account and database, then cleans both up.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# The store prefers host.docker.internal first (for the container deployment);
# on the host that just stalls, so point it at the published PG port.
os.environ["QWENPAW_DATA_TABLES_DB_HOST"] = "127.0.0.1"
os.environ.setdefault("QWENPAW_DATA_TABLES_DB_PORT", "15432")
os.environ["QWENPAW_DATA_TABLES_DB_NAME"] = "huizhi_knowledge"
os.environ["QWENPAW_DATA_TABLES_DB_USER"] = "huizhi_knowledge"
os.environ["QWENPAW_DATA_TABLES_DB_PASSWORD"] = "huizhi_knowledge"

from qwenpaw.app.agent_context import set_current_user_id  # noqa: E402
from qwenpaw.config.context import set_current_workspace_dir  # noqa: E402
from qwenpaw.agents.tools import data_tables  # noqa: E402
from qwenpaw.data_tables import store as dt_store  # noqa: E402

USER = "e2e_verify_20260918"
OTHER_USER = "e2e_verify_other"
CSV_TEXT = "region,amount\n华东,100\n华南,200\n华北,300\n"


def _purge_accounts(usernames: list[str]) -> None:
    """Remove registry rows, mapping rows and per-user databases."""
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(
        host="127.0.0.1",
        port=15432,
        dbname="huizhi_knowledge",
        user="huizhi_knowledge",
        password="huizhi_knowledge",
    )
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT DISTINCT db_name FROM {dt_store.SYSTEM_TABLE_REGISTRY}"
                f" WHERE owner_username = ANY(%s)",
                (usernames,),
            )
            databases = [row["db_name"] for row in cur.fetchall() if row["db_name"]]
            # Import jobs cascade from the registry row.
            cur.execute(
                f"DELETE FROM {dt_store.SYSTEM_TABLE_REGISTRY} WHERE owner_username = ANY(%s)",
                (usernames,),
            )
            cur.execute(
                f"DELETE FROM {dt_store.SYSTEM_TABLE_USER_DATABASE} WHERE username = ANY(%s)",
                (usernames,),
            )
            conn.commit()
            for database in databases:
                # DROP DATABASE cannot run inside a transaction block.
                conn.autocommit = True
                cur.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
                conn.autocommit = False
    finally:
        conn.close()


def simulate_chat_upload(workspace: Path, original_name: str, text: str) -> Path:
    """Recreate exactly how the console stores an attachment."""
    media = workspace / "media"
    media.mkdir(parents=True, exist_ok=True)
    stored = media / f"{'f' * 32}_{original_name}"
    stored.write_text(text, encoding="utf-8")
    return stored


async def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="e2e_ws_"))
    # The staging dir is the container's /app/working path; redirect it here.
    staging = Path(tempfile.mkdtemp(prefix="e2e_staging_"))
    dt_store.UPLOAD_DIR = str(staging)

    failures: list[str] = []
    table_name = ""
    try:
        upload = simulate_chat_upload(workspace, "销售数据.csv", CSV_TEXT)
        outside = workspace.parent / "e2e_outside.csv"
        outside.write_text("a,b\n1,2\n", encoding="utf-8")

        set_current_user_id(USER)
        set_current_workspace_dir(str(workspace))

        # 1. create the table through the tool (no column list needed)
        created = await data_tables.create_data_table("端到端验证销售明细")
        print("create:", created.replace("\n", " "))
        match = re.search(r"(tb_hzxzs_\w+)", created)
        if not match:
            print("RESULT: FAIL (no table name returned)")
            return 1
        table_name = match.group(1)

        # 2. import the attachment
        imported = await data_tables.import_data_table(table_name, "销售数据.csv")
        print("import:", imported.replace("\n", " "))

        # 3. the attachment must survive the import
        survived = upload.exists()
        print("attachment_survived:", survived)
        if not survived:
            failures.append("chat attachment was consumed by the import")

        # 4. row count via the existing read-only tool (sync on purpose)
        counted = data_tables.query_data_table(f"SELECT count(*) FROM {table_name}")
        print("count:", counted.replace("\n", " "))
        if "3" not in counted:
            failures.append(f"expected 3 imported rows, got: {counted!r}")

        # 5. a file outside the workspace must be refused
        rejected = await data_tables.import_data_table(table_name, str(outside))
        print("outside_path:", rejected.replace("\n", " "))
        if "不在你的工作区内" not in rejected:
            failures.append(f"outside path was not refused: {rejected!r}")

        # 6. another account's table must not be reachable
        set_current_user_id(OTHER_USER)
        foreign = await data_tables.import_data_table(table_name, "销售数据.csv")
        print("foreign_table:", foreign.replace("\n", " "))
        if "没有找到属于你的数据表" not in foreign:
            failures.append(f"cross-account import was not refused: {foreign!r}")
        set_current_user_id(USER)
    finally:
        _purge_accounts([USER, OTHER_USER])
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
        print("cleaned up scratch accounts, workspace and staging")

    if failures:
        for item in failures:
            print("FAILURE:", item)
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
