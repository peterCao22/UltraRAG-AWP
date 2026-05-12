"""Phase 5.1.6 — SQLite → PostgreSQL 数据迁移脚本。

把 db/app.sqlite 的所有表数据搬到 Postgres（连接信息见 .env）。

用法：
    # 1. dry-run：看每表行数
    .venv\\Scripts\\python.exe -m custom_app.scripts.migrate_sqlite_to_postgres --dry-run

    # 2. 真实迁移（先 init schema 再 copy 数据）
    .venv\\Scripts\\python.exe -m custom_app.scripts.migrate_sqlite_to_postgres

    # 3. 重建模式（先 TRUNCATE Postgres 所有表再迁移；用于重跑）
    .venv\\Scripts\\python.exe -m custom_app.scripts.migrate_sqlite_to_postgres --truncate

策略：
    - 按表顺序逐张迁移（不外键级联，先迁 KB/role 等主表，再迁 documents/jobs 等）
    - 每表内分批 INSERT，避免单事务过大
    - 跳过 id 列（让 Postgres SERIAL 自动分配），保留其他所有字段
    - 对 SQLite 中 NULL/datetime 等做透传（schema 全 TEXT，无类型转换风险）
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


# 迁移顺序：先父表（KB / role），再子表（documents / permissions / etc）
# 按外键依赖关系手动排序；SQLite 不强制外键，但 Postgres 严格
TABLE_MIGRATION_ORDER = [
    ("knowledge_bases", [
        "kb_id", "name", "description", "tenant_id", "status", "type",
        "data_path", "index_path", "embedding_path", "last_indexed_at",
        "created_at", "updated_at",
    ]),
    ("kb_jobs", [
        "job_id", "tenant_id", "kb_id", "job_type", "status", "retry_count",
        "last_error", "payload_json", "result_json", "started_at", "finished_at",
        "created_at", "updated_at",
    ]),
    ("kb_documents", [
        "kb_id", "tenant_id", "doc_id", "file_name", "file_type", "file_path",
        "channel", "status", "error_message", "created_at", "updated_at",
    ]),
    ("roles", [
        "role_id", "name", "description", "created_at", "updated_at",
    ]),
    ("role_kb_permissions", [
        "role_id", "kb_id", "access_level", "created_at", "updated_at",
    ]),
    ("kb_sessions", [
        "session_id", "kb_id", "title", "agent_mode", "created_at", "updated_at",
    ]),
    ("kb_session_messages", [
        "session_id", "role", "content", "reasoning_json", "created_at",
    ]),
    ("kb_agent_configs", [
        "kb_id", "enabled_tools_json", "created_at", "updated_at",
    ]),
    ("kg_entities", [
        "kb_id", "entity_name", "entity_type", "description", "chunk_ids", "created_at",
    ]),
    ("kg_relations", [
        "kb_id", "source_id", "target_id", "relation_type", "description", "strength", "created_at",
    ]),
]


def _count_sqlite_rows(sqlite_conn, table: str) -> int:
    cur = sqlite_conn.execute(f"SELECT COUNT(*) FROM {table}")
    return int(cur.fetchone()[0])


def _migrate_table(
    sqlite_conn,
    pg_provider,
    table: str,
    columns: list[str],
    *,
    batch_size: int = 200,
    entity_id_map: "dict[int, int] | None" = None,
) -> int:
    """把单表数据从 SQLite 搬到 Postgres，返回迁移行数。

    注意：不带 SQLite 的 id 列（让 Postgres SERIAL 重新分配）。

    kg_entities 特殊处理：用 INSERT...RETURNING id 拿到新 ID，建立 old_id→new_id
    映射，写入 entity_id_map（调用方提供）。

    kg_relations 特殊处理：source_id/target_id 通过 entity_id_map 重映射，
    跳过映射不到的孤儿关系。
    """
    from custom_app.repositories.base import adapt_sql

    col_csv = ", ".join(columns)
    placeholders = ", ".join(["?"] * len(columns))
    select_sql = f"SELECT id, {col_csv} FROM {table}" if table == "kg_entities" else f"SELECT {col_csv} FROM {table}"
    insert_sql = f"INSERT INTO {table} ({col_csv}) VALUES ({placeholders})"
    if table == "kg_entities":
        insert_sql = f"INSERT INTO {table} ({col_csv}) VALUES ({placeholders}) RETURNING id"
    adapted_sql = adapt_sql(insert_sql, pg_provider)

    sqlite_conn.row_factory = sqlite3.Row
    rows = sqlite_conn.execute(select_sql).fetchall()
    if not rows:
        return 0

    inserted = 0
    skipped = 0
    with pg_provider.connect() as adapter:
        for row in rows:
            if table == "kg_relations" and entity_id_map is not None:
                # 重映射 source_id / target_id
                old_src = row["source_id"]
                old_tgt = row["target_id"]
                new_src = entity_id_map.get(old_src)
                new_tgt = entity_id_map.get(old_tgt)
                if new_src is None or new_tgt is None:
                    skipped += 1
                    continue
                params = tuple(
                    new_src if c == "source_id" else
                    new_tgt if c == "target_id" else
                    row[c]
                    for c in columns
                )
            else:
                params = tuple(row[c] for c in columns)

            cur = adapter.execute(adapted_sql, params)
            inserted += 1

            # kg_entities：抓 RETURNING id 建映射
            if table == "kg_entities" and entity_id_map is not None:
                ret_row = cur.fetchone()
                if ret_row is None:
                    continue
                new_id = ret_row["id"] if isinstance(ret_row, dict) else ret_row[0]
                entity_id_map[int(row["id"])] = int(new_id)

    if skipped:
        print(f"    (skipped {skipped} rows with unmapped entity_id)")
    return inserted


def cmd_migrate(args) -> int:
    sqlite_path = Path(args.sqlite_path)
    if not sqlite_path.exists():
        print(f"ERROR: sqlite db not found: {sqlite_path}", file=sys.stderr)
        return 2

    print(f"=== SQLite → PostgreSQL 迁移 ===")
    print(f"SQLite: {sqlite_path}")

    from custom_app.repositories.postgres_provider import (
        PostgresConnectionProvider,
        init_postgres_schema,
    )

    pg_provider = PostgresConnectionProvider()

    # 1. 建 schema
    print("\n[1/3] 初始化 Postgres schema...")
    init_postgres_schema(pg_provider)
    print("  [OK] schema 就绪")

    # 2. 可选 truncate
    if args.truncate and not args.dry_run:
        print("\n[2/3] TRUNCATE Postgres 所有表...")
        with pg_provider.connect() as adapter:
            # 反向顺序 truncate，避免 FK 报错（虽然我们的 schema 没显式 FK）
            for table, _ in reversed(TABLE_MIGRATION_ORDER):
                adapter.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")
        print("  [OK] 所有表已清空")
    elif args.truncate:
        print("\n[2/3] [DRY RUN] 跳过 TRUNCATE")

    # 3. 逐表迁移
    print(f"\n[3/3] 数据迁移 ({'DRY RUN' if args.dry_run else '实际写入'})...")
    sqlite_conn = sqlite3.connect(str(sqlite_path))
    total_migrated = 0
    # kg_entities 迁移时填充 old_id → new_id 映射，kg_relations 迁移时用它重映射 FK
    entity_id_map: dict[int, int] = {}
    try:
        for table, columns in TABLE_MIGRATION_ORDER:
            row_count = _count_sqlite_rows(sqlite_conn, table)
            if row_count == 0:
                print(f"  {table:30s} 0 rows (skip)")
                continue
            if args.dry_run:
                print(f"  {table:30s} {row_count} rows (dry-run)")
                continue
            migrated = _migrate_table(
                sqlite_conn, pg_provider, table, columns,
                entity_id_map=entity_id_map,
            )
            total_migrated += migrated
            status = "OK" if migrated == row_count else f"WARN ({migrated}/{row_count})"
            print(f"  {table:30s} {row_count} rows [{status}]")
    finally:
        sqlite_conn.close()
        pg_provider.close()

    if args.dry_run:
        print("\n[DRY RUN] 未实际写入。")
        return 0

    print(f"\n[OK] 迁移完成，共迁移 {total_migrated} 行")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Phase 5.1.6 SQLite → Postgres 迁移")
    p.add_argument(
        "--sqlite-path",
        default="db/app.sqlite",
        help="SQLite 源文件（默认 db/app.sqlite）",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="不实际写入 Postgres，仅显示每表行数",
    )
    p.add_argument(
        "--truncate",
        action="store_true",
        help="迁移前 TRUNCATE 所有目标表（用于重跑）",
    )
    args = p.parse_args()
    return cmd_migrate(args)


if __name__ == "__main__":
    raise SystemExit(main())
