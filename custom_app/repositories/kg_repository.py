"""KgRepository —— kg_entities + kg_relations 表（知识图谱）。

业务方法：
    find_entity_by_name(kb_id, entity_name)
    upsert_entity(...)
    insert_relation(...)
    find_relation(kb_id, source_id, target_id, relation_type)
    update_entity_chunks(entity_id, chunk_ids_json)
    delete_all_for_kb(kb_id)
    count_entities_and_relations(kb_id?)
    find_relations_for_entities(kb_id, entity_names) —— 用于 kg_search
"""

from __future__ import annotations

from typing import Any, Optional

from custom_app.repositories.base import (
    ConnectionProvider,
    adapt_sql,
    fetch_all_as_dicts,
    fetch_one_as_dict,
    get_default_provider,
)


class KgRepository:
    def __init__(self, provider: Optional[ConnectionProvider] = None) -> None:
        self._provider = provider or get_default_provider()

    # ------------------------------------------------------------------
    # kg_entities
    # ------------------------------------------------------------------

    def find_entity_by_name(
        self, kb_id: str, entity_name: str
    ) -> Optional[dict[str, Any]]:
        sql = "SELECT id, chunk_ids FROM kg_entities WHERE kb_id=? AND entity_name=?"
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (kb_id, entity_name))
            return fetch_one_as_dict(cur)

    def insert_entity(
        self,
        *,
        kb_id: str,
        entity_name: str,
        entity_type: str,
        description: str,
        chunk_ids_json: str,
        created_at: str,
    ) -> int:
        """插入实体，返回新 id。

        SQLite 用 cur.lastrowid；Postgres 用 RETURNING id（SQLite 3.35+ 也支持）。
        统一走 RETURNING id 以保持两种后端的接口一致。
        """
        sql = (
            "INSERT INTO kg_entities (kb_id, entity_name, entity_type, description, chunk_ids, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) RETURNING id"
        )
        with self._provider.connect() as conn:
            cur = conn.execute(
                adapt_sql(sql, self._provider),
                (kb_id, entity_name, entity_type, description, chunk_ids_json, created_at),
            )
            row = cur.fetchone()
            if row is None:
                return 0
            # SQLite Row 支持索引访问；psycopg dict_row 返回 dict
            if hasattr(row, "keys") and not isinstance(row, dict):
                return int(row["id"])
            if isinstance(row, dict):
                return int(row["id"])
            return int(row[0])

    def update_entity_chunks(self, entity_id: int, *, chunk_ids_json: str) -> None:
        sql = "UPDATE kg_entities SET chunk_ids=? WHERE id=?"
        with self._provider.connect() as conn:
            conn.execute(adapt_sql(sql, self._provider), (chunk_ids_json, entity_id))

    # ------------------------------------------------------------------
    # kg_relations
    # ------------------------------------------------------------------

    def find_relation(
        self,
        *,
        kb_id: str,
        source_id: int,
        target_id: int,
        relation_type: str,
    ) -> Optional[dict[str, Any]]:
        sql = (
            "SELECT id FROM kg_relations "
            "WHERE kb_id=? AND source_id=? AND target_id=? AND relation_type=?"
        )
        with self._provider.connect() as conn:
            cur = conn.execute(
                adapt_sql(sql, self._provider),
                (kb_id, source_id, target_id, relation_type),
            )
            return fetch_one_as_dict(cur)

    def insert_relation(
        self,
        *,
        kb_id: str,
        source_id: int,
        target_id: int,
        relation_type: str,
        description: str,
        strength: int,
        created_at: str,
    ) -> None:
        sql = (
            "INSERT INTO kg_relations "
            "(kb_id, source_id, target_id, relation_type, description, strength, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)"
        )
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (kb_id, source_id, target_id, relation_type, description, strength, created_at),
            )

    # ------------------------------------------------------------------
    # 批量 / 统计
    # ------------------------------------------------------------------

    def delete_all_for_kb(self, kb_id: str) -> tuple[int, int]:
        """删除 kb_id 下所有实体+关系；返回 (relations_count, entities_count)。

        用别名 AS cnt 让 SQLite Row 和 Postgres dict_row 都能用 dict 方式取值，
        避免后端差异（SQLite Row 支持数字索引，Postgres dict_row 不支持）。
        """
        with self._provider.connect() as conn:
            rel_count_cur = conn.execute(
                adapt_sql("SELECT COUNT(*) AS cnt FROM kg_relations WHERE kb_id=?", self._provider),
                (kb_id,),
            )
            rel_row = fetch_one_as_dict(rel_count_cur)
            rel_count = int(rel_row["cnt"]) if rel_row else 0

            ent_count_cur = conn.execute(
                adapt_sql("SELECT COUNT(*) AS cnt FROM kg_entities WHERE kb_id=?", self._provider),
                (kb_id,),
            )
            ent_row = fetch_one_as_dict(ent_count_cur)
            ent_count = int(ent_row["cnt"]) if ent_row else 0

            conn.execute(
                adapt_sql("DELETE FROM kg_relations WHERE kb_id=?", self._provider),
                (kb_id,),
            )
            conn.execute(
                adapt_sql("DELETE FROM kg_entities WHERE kb_id=?", self._provider),
                (kb_id,),
            )
            return rel_count, ent_count

    def count_entities_and_relations(
        self, kb_id: Optional[str] = None
    ) -> dict[str, Any]:
        """统计实体和关系数；kb_id=None 时统计全局。"""
        if kb_id:
            sql = (
                "SELECT COUNT(DISTINCT e.id) as ec, COUNT(DISTINCT r.id) as rc "
                "FROM kg_entities e LEFT JOIN kg_relations r ON r.source_id = e.id "
                "WHERE e.kb_id = ?"
            )
            params: tuple = (kb_id,)
        else:
            sql = (
                "SELECT COUNT(DISTINCT e.id) as ec, COUNT(DISTINCT r.id) as rc "
                "FROM kg_entities e LEFT JOIN kg_relations r ON r.source_id = e.id"
            )
            params = ()
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), params)
            row = fetch_one_as_dict(cur)
            return {
                "kb_id": kb_id or "all",
                "entity_count": (row or {}).get("ec", 0) if row else 0,
                "relation_count": (row or {}).get("rc", 0) if row else 0,
            }

    # ------------------------------------------------------------------
    # 复杂查询：用于 kg_search 的 UNION ALL（双向邻居）
    # ------------------------------------------------------------------

    def find_relations_for_entities(
        self, kb_id: str, entity_names: list[str]
    ) -> list[dict[str, Any]]:
        """查找种子实体集的 outgoing + incoming 关系，含邻居实体信息。

        返回行包含统一字段：entity_id / entity_name / entity_type / description /
        chunk_ids / direction / relation_type / description_rel / strength /
        neighbor_chunks / source_name / target_name
        """
        if not entity_names:
            return []
        placeholders = ",".join(["?"] * len(entity_names))
        sql = f"""
            -- seed 段：种子实体自身（无关系，作为兜底）
            SELECT e.id as entity_id, e.entity_name, e.entity_type, e.description,
                   e.chunk_ids, 'self' as direction,
                   NULL as relation_type, NULL as description_rel, NULL as strength,
                   NULL as neighbor_chunks,
                   NULL as source_name, NULL as target_name
            FROM kg_entities e
            WHERE e.kb_id = ? AND e.entity_name IN ({placeholders})

            UNION ALL

            -- outgoing：种子在 e（source），邻居在 t（target）
            SELECT t.id as entity_id, t.entity_name, t.entity_type, t.description,
                   t.chunk_ids, 'source' as direction,
                   r.relation_type, r.description as description_rel, r.strength,
                   e.chunk_ids as neighbor_chunks,
                   e.entity_name as source_name, t.entity_name as target_name
            FROM kg_entities e
            JOIN kg_relations r ON r.source_id = e.id
            JOIN kg_entities t ON t.id = r.target_id
            WHERE e.kb_id = ? AND e.entity_name IN ({placeholders})

            UNION ALL

            -- incoming：种子在 t（target），邻居在 e（source）
            SELECT e.id as entity_id, e.entity_name, e.entity_type, e.description,
                   e.chunk_ids, 'target' as direction,
                   r.relation_type, r.description as description_rel, r.strength,
                   t.chunk_ids as neighbor_chunks,
                   e.entity_name as source_name, t.entity_name as target_name
            FROM kg_entities t
            JOIN kg_relations r ON r.target_id = t.id
            JOIN kg_entities e ON e.id = r.source_id
            WHERE t.kb_id = ? AND t.entity_name IN ({placeholders})
        """
        params = [kb_id] + list(entity_names) + [kb_id] + list(entity_names) + [kb_id] + list(entity_names)
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), tuple(params))
            return fetch_all_as_dicts(cur)
