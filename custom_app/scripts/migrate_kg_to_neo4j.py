"""Phase 5.2 — SQLite/Postgres kg_entities/kg_relations → Neo4j 迁移脚本。

从当前 ULTRARAG_DB_BACKEND（sqlite 或 postgres）读 KG 表，
按 kb_id 分组写入 Neo4j。

Phase 5.1 已经把 SQLite int ID 重映射到 Postgres SERIAL；本脚本把
SQLite/Postgres int ID 重映射到 Neo4j element_id。

用法：
    # dry-run：查看将要迁移的实体/关系数
    .venv\\Scripts\\python.exe -m custom_app.scripts.migrate_kg_to_neo4j --dry-run

    # 真实迁移（默认覆盖：先 delete_all_for_kb 再迁入）
    .venv\\Scripts\\python.exe -m custom_app.scripts.migrate_kg_to_neo4j

    # 仅迁移特定 KB
    .venv\\Scripts\\python.exe -m custom_app.scripts.migrate_kg_to_neo4j --kb ifs_docs

退出码：
    0  迁移完成
    1  失败
    2  参数错误
"""

from __future__ import annotations

import argparse
import sys
from typing import Iterable

from dotenv import load_dotenv

load_dotenv()


def _list_all_kbs(kg_repo) -> list[str]:
    """从当前默认 SQL 后端拿到所有有 KG 数据的 kb_id 列表。"""
    # KgRepository 没有 list_all_kbs；用统计接口绕过：先看 count_*(None) 看总实体
    # 这里更简单：直接走 base provider 跑 SQL
    from custom_app.repositories.base import (
        adapt_sql,
        fetch_all_as_dicts,
        get_default_provider,
    )

    provider = get_default_provider()
    with provider.connect() as conn:
        cur = conn.execute(
            adapt_sql("SELECT DISTINCT kb_id FROM kg_entities ORDER BY kb_id", provider)
        )
        rows = fetch_all_as_dicts(cur)
        return [r["kb_id"] for r in rows]


def _fetch_entities_for_kb(kb_id: str) -> list[dict]:
    """读某 KB 的全部实体（含 id 用于 FK 映射）。"""
    from custom_app.repositories.base import (
        adapt_sql,
        fetch_all_as_dicts,
        get_default_provider,
    )

    provider = get_default_provider()
    with provider.connect() as conn:
        cur = conn.execute(
            adapt_sql(
                "SELECT id, kb_id, entity_name, entity_type, description, chunk_ids, created_at "
                "FROM kg_entities WHERE kb_id = ? ORDER BY id",
                provider,
            ),
            (kb_id,),
        )
        return fetch_all_as_dicts(cur)


def _fetch_relations_for_kb(kb_id: str) -> list[dict]:
    """读某 KB 的全部关系。"""
    from custom_app.repositories.base import (
        adapt_sql,
        fetch_all_as_dicts,
        get_default_provider,
    )

    provider = get_default_provider()
    with provider.connect() as conn:
        cur = conn.execute(
            adapt_sql(
                "SELECT kb_id, source_id, target_id, relation_type, "
                "description, strength, created_at "
                "FROM kg_relations WHERE kb_id = ? ORDER BY id",
                provider,
            ),
            (kb_id,),
        )
        return fetch_all_as_dicts(cur)


def _migrate_kb(neo4j_store, kb_id: str, *, dry_run: bool) -> tuple[int, int, int]:
    """迁移单个 KB 的 KG 数据；返回 (entity_count, rel_count, skipped)。

    skipped = 因 source/target 映射不到而丢弃的关系数。
    """
    entities = _fetch_entities_for_kb(kb_id)
    relations = _fetch_relations_for_kb(kb_id)

    if dry_run:
        print(f"  [DRY] {kb_id}: {len(entities)} entities, {len(relations)} relations")
        return len(entities), len(relations), 0

    # 1. 清除 Neo4j 中已有的同 kb_id 数据（覆盖语义）
    rc, ec = neo4j_store.delete_all_for_kb(kb_id)
    if rc or ec:
        print(f"  cleared existing Neo4j {kb_id}: {ec} entities, {rc} relations")

    # 2. 写实体，建立 old_int_id → new_element_id 映射
    id_map: dict[int, str] = {}
    for ent in entities:
        new_id = neo4j_store.insert_entity(
            kb_id=ent["kb_id"],
            entity_name=ent["entity_name"],
            entity_type=ent["entity_type"],
            description=ent.get("description") or "",
            chunk_ids_json=ent.get("chunk_ids") or "[]",
            created_at=ent["created_at"],
        )
        id_map[int(ent["id"])] = new_id

    # 3. 写关系（重映射 source_id / target_id）
    skipped = 0
    inserted = 0
    for rel in relations:
        old_src = int(rel["source_id"])
        old_tgt = int(rel["target_id"])
        new_src = id_map.get(old_src)
        new_tgt = id_map.get(old_tgt)
        if new_src is None or new_tgt is None:
            skipped += 1
            continue
        neo4j_store.insert_relation(
            kb_id=rel["kb_id"],
            source_id=new_src,
            target_id=new_tgt,
            relation_type=rel["relation_type"],
            description=rel.get("description") or "",
            strength=int(rel.get("strength") or 5),
            created_at=rel["created_at"],
        )
        inserted += 1

    return len(entities), inserted, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 5.2 KG→Neo4j 迁移")
    parser.add_argument(
        "--kb",
        default="",
        help="仅迁移指定 kb_id；默认迁移所有有 KG 数据的 KB",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="不实际写 Neo4j，仅打印每 KB 行数",
    )
    args = parser.parse_args()

    print("=== Phase 5.2 KG → Neo4j 迁移 ===")

    # 探测后端
    import os
    db_backend = os.environ.get("ULTRARAG_DB_BACKEND", "sqlite").lower()
    print(f"  Source backend: {db_backend}")
    print(f"  Target: Neo4j @ {os.environ.get('ULTRARAG_NEO4J_URI', '(unset)')}")

    if args.kb:
        kb_list = [args.kb.strip()]
    else:
        try:
            kb_list = _list_all_kbs(None)
        except Exception as e:
            print(f"ERROR: 列 KB 失败：{e}", file=sys.stderr)
            return 1

    if not kb_list:
        print("  (no KBs with KG data)")
        return 0
    print(f"  KBs to migrate: {kb_list}")

    # 构造 Neo4j store
    try:
        from custom_app.services.kgstore.neo4j_store import Neo4jKgStore
        neo4j_store = Neo4jKgStore()
        neo4j_store.ensure_constraints()
    except Exception as e:
        print(f"ERROR: Neo4j 连接失败：{e}", file=sys.stderr)
        return 1

    total_e = 0
    total_r = 0
    total_skipped = 0
    try:
        for kb_id in kb_list:
            ec, rc, sk = _migrate_kb(neo4j_store, kb_id, dry_run=args.dry_run)
            tag = "DRY" if args.dry_run else "OK"
            print(f"  [{tag}] {kb_id}: {ec} entities, {rc} relations (skipped {sk})")
            total_e += ec
            total_r += rc
            total_skipped += sk
    finally:
        neo4j_store.close()

    print(f"\n总计：{total_e} entities + {total_r} relations" +
          (f"，skipped {total_skipped}" if total_skipped else ""))
    if args.dry_run:
        print("[DRY RUN] 未实际写入。")
    else:
        print("[OK] 迁移完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
