"""Neo4jKgStore —— KgStore Protocol 的 Neo4j 实现（Phase 5.2）。

部署要求：
    - Neo4j 5+ 服务（局域网 Docker，连接信息见 .env 中 ULTRARAG_NEO4J_*）
    - neo4j Python driver >= 5（已加入 pyproject.toml [storage] extras）

Graph 模型设计：
    节点：(:Entity {kb_id, name, entity_type, description, chunk_ids, created_at})
    关系：(:Entity)-[:RELATES_TO {kb_id, relation_type, description, strength, created_at}]->(:Entity)

    单 database 模式（Community 版兼容）：所有 KB 共享 'neo4j' database，
    用节点 property `kb_id` 区分；约束 (kb_id, name) 组合唯一。

    entity_id 用 Neo4j 的 element_id（字符串）做业务标识，跨 cluster / 持久化稳定。

约束 / 索引：
    - UNIQUE (kb_id, name) on :Entity     —— 防止同 KB 内重复实体
    - INDEX :Entity(kb_id)                —— 加速按 KB 过滤
    - INDEX :RELATES_TO(kb_id)            —— 加速关系过滤
    （ensure_constraints 在首次连接时幂等创建）
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


class Neo4jKgStore:
    """Neo4j 后端：单 database + kb_id property 区分 KB。"""

    def __init__(
        self,
        *,
        uri: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
    ) -> None:
        self._uri = uri or os.environ.get("ULTRARAG_NEO4J_URI", "")
        self._user = user or os.environ.get("ULTRARAG_NEO4J_USER", "neo4j")
        self._password = password or os.environ.get("ULTRARAG_NEO4J_PASSWORD", "")
        self._database = database or os.environ.get("ULTRARAG_NEO4J_DATABASE", "neo4j")

        if not self._uri:
            raise ValueError("ULTRARAG_NEO4J_URI not set in environment")

        self._driver = self._build_driver()
        self._ensured_schema = False

    def _build_driver(self):
        from neo4j import GraphDatabase  # type: ignore

        return GraphDatabase.driver(self._uri, auth=(self._user, self._password))

    def _session(self):
        return self._driver.session(database=self._database)

    def ensure_constraints(self) -> None:
        """幂等创建约束 + 索引（首次连接时调用）。"""
        if self._ensured_schema:
            return
        with self._session() as session:
            # UNIQUE constraint on (kb_id, name)：保证同 KB 内同名实体只有一个
            session.run(
                """
                CREATE CONSTRAINT entity_kb_name_unique IF NOT EXISTS
                FOR (e:Entity) REQUIRE (e.kb_id, e.name) IS UNIQUE
                """
            )
            # 索引加速 kb_id 过滤
            session.run(
                "CREATE INDEX entity_kb_id IF NOT EXISTS FOR (e:Entity) ON (e.kb_id)"
            )
            session.run(
                "CREATE INDEX rel_kb_id IF NOT EXISTS "
                "FOR ()-[r:RELATES_TO]-() ON (r.kb_id)"
            )
        self._ensured_schema = True
        logger.info("neo4j schema constraints/indexes ensured")

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()

    # ------------------------------------------------------------------
    # KgStore Protocol
    # ------------------------------------------------------------------

    def find_entity_by_name(self, kb_id: str, entity_name: str):
        from custom_app.services.kgstore.base import EntityRecord

        self.ensure_constraints()
        with self._session() as session:
            rec = session.run(
                """
                MATCH (e:Entity {kb_id: $kb_id, name: $name})
                RETURN elementId(e) AS id, e.chunk_ids AS chunk_ids
                """,
                kb_id=kb_id, name=entity_name,
            ).single()
            if rec is None:
                return None
            return EntityRecord(id=str(rec["id"]), chunk_ids=rec["chunk_ids"] or "[]")

    def insert_entity(
        self,
        *,
        kb_id: str,
        entity_name: str,
        entity_type: str,
        description: str,
        chunk_ids_json: str,
        created_at: str,
    ) -> str:
        self.ensure_constraints()
        with self._session() as session:
            # 用 CREATE 而非 MERGE：上层在 find_entity_by_name 已确认不存在
            # （MERGE 会在已有时更新，可能掩盖逻辑 bug）
            rec = session.run(
                """
                CREATE (e:Entity {
                    kb_id: $kb_id,
                    name: $name,
                    entity_type: $entity_type,
                    description: $description,
                    chunk_ids: $chunk_ids,
                    created_at: $created_at
                })
                RETURN elementId(e) AS id
                """,
                kb_id=kb_id,
                name=entity_name,
                entity_type=entity_type,
                description=description,
                chunk_ids=chunk_ids_json,
                created_at=created_at,
            ).single()
            return str(rec["id"])

    def update_entity_full(
        self,
        entity_id: str,
        *,
        entity_type: str,
        description: str,
        chunk_ids_json: str,
    ) -> None:
        self.ensure_constraints()
        with self._session() as session:
            session.run(
                """
                MATCH (e:Entity) WHERE elementId(e) = $id
                SET e.entity_type = $entity_type,
                    e.description = $description,
                    e.chunk_ids = $chunk_ids
                """,
                id=entity_id,
                entity_type=entity_type,
                description=description,
                chunk_ids=chunk_ids_json,
            )

    def find_relation(
        self,
        *,
        kb_id: str,
        source_id: str,
        target_id: str,
        relation_type: str,
    ) -> Optional[dict[str, Any]]:
        self.ensure_constraints()
        with self._session() as session:
            rec = session.run(
                """
                MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity)
                WHERE elementId(s) = $sid
                  AND elementId(t) = $tid
                  AND r.kb_id = $kb_id
                  AND r.relation_type = $rtype
                RETURN elementId(r) AS id
                """,
                sid=source_id, tid=target_id,
                kb_id=kb_id, rtype=relation_type,
            ).single()
            if rec is None:
                return None
            return {"id": rec["id"]}

    def insert_relation(
        self,
        *,
        kb_id: str,
        source_id: str,
        target_id: str,
        relation_type: str,
        description: str,
        strength: int,
        created_at: str,
        doc_id: str = "",
    ) -> None:
        """Phase 6.2: doc_id 标记关系来源文档，便于按 doc 删除。"""
        self.ensure_constraints()
        with self._session() as session:
            session.run(
                """
                MATCH (s:Entity) WHERE elementId(s) = $sid
                MATCH (t:Entity) WHERE elementId(t) = $tid
                CREATE (s)-[r:RELATES_TO {
                    kb_id: $kb_id,
                    relation_type: $rtype,
                    description: $description,
                    strength: $strength,
                    doc_id: $doc_id,
                    created_at: $created_at
                }]->(t)
                """,
                sid=source_id, tid=target_id,
                kb_id=kb_id, rtype=relation_type,
                description=description, strength=strength,
                doc_id=doc_id,
                created_at=created_at,
            )

    def delete_by_doc(self, kb_id: str, doc_id: str) -> tuple[int, int]:
        """Phase 6.2: 删除某 doc 的 KG 数据。

        步骤：
          1. DELETE r WHERE r.kb_id=$kb_id AND r.doc_id=$doc_id
          2. 找该 doc 的实体（chunk_ids 包含 doc_stem 前缀的 chunk id），把这些
             chunk_id 从 chunk_ids JSON 数组里移除；剩余为空则连实体一起 DETACH DELETE
        老数据 doc_id='' 的不受影响。返回 (rel_deleted, ent_deleted)。
        """
        if not doc_id:
            return 0, 0
        self.ensure_constraints()
        import json as _json
        from custom_app.utils.chunks_io import doc_id_to_stem

        doc_stem = doc_id_to_stem(doc_id)

        with self._session() as session:
            # 1) 删关系并统计
            rel_rec = session.run(
                """
                MATCH ()-[r:RELATES_TO {kb_id: $kb_id, doc_id: $doc_id}]->()
                WITH count(r) AS cnt
                CALL {
                  MATCH ()-[r2:RELATES_TO {kb_id: $kb_id, doc_id: $doc_id}]->()
                  DELETE r2
                  RETURN count(*) AS deleted
                }
                RETURN cnt
                """,
                kb_id=kb_id, doc_id=doc_id,
            ).single()
            rel_deleted = int(rel_rec["cnt"]) if rel_rec else 0

            # 2) 找该 KB 的所有实体，剔除属于该 doc_stem 的 chunk ids
            ent_records = session.run(
                "MATCH (e:Entity {kb_id: $kb_id}) RETURN elementId(e) AS id, e.chunk_ids AS chunk_ids",
                kb_id=kb_id,
            ).data()

            ent_deleted = 0
            for rec in ent_records:
                eid = rec["id"]
                raw = rec.get("chunk_ids") or "[]"
                try:
                    chunk_ids = _json.loads(raw)
                except Exception:
                    chunk_ids = []
                # chunk.id 形如 "{doc_stem}_section_N"；按前缀过滤
                kept = [cid for cid in chunk_ids if not str(cid).startswith(f"{doc_stem}_")]
                if len(kept) == len(chunk_ids):
                    continue
                if not kept:
                    session.run(
                        "MATCH (e:Entity) WHERE elementId(e) = $id DETACH DELETE e",
                        id=eid,
                    )
                    ent_deleted += 1
                else:
                    session.run(
                        "MATCH (e:Entity) WHERE elementId(e) = $id SET e.chunk_ids = $chunk_ids",
                        id=eid, chunk_ids=_json.dumps(kept),
                    )

        return rel_deleted, ent_deleted

    def delete_all_for_kb(self, kb_id: str) -> tuple[int, int]:
        """删除某 KB 下所有节点+关系，返回 (rel_count, ent_count)。"""
        self.ensure_constraints()
        with self._session() as session:
            # 先 count 再 detach delete（DETACH 自动级联删除附属关系）
            rec = session.run(
                """
                MATCH (e:Entity {kb_id: $kb_id})
                OPTIONAL MATCH (e)-[r:RELATES_TO]->()
                WHERE r.kb_id = $kb_id
                RETURN count(DISTINCT e) AS ec, count(DISTINCT r) AS rc
                """,
                kb_id=kb_id,
            ).single()
            ec = int(rec["ec"]) if rec else 0
            rc = int(rec["rc"]) if rec else 0

            session.run(
                "MATCH (e:Entity {kb_id: $kb_id}) DETACH DELETE e",
                kb_id=kb_id,
            )
            return rc, ec

    def count_entities_and_relations(
        self, kb_id: Optional[str] = None
    ) -> dict[str, Any]:
        self.ensure_constraints()
        with self._session() as session:
            if kb_id:
                rec = session.run(
                    """
                    MATCH (e:Entity {kb_id: $kb_id})
                    OPTIONAL MATCH (e)-[r:RELATES_TO]->()
                    WHERE r.kb_id = $kb_id
                    RETURN count(DISTINCT e) AS ec, count(DISTINCT r) AS rc
                    """,
                    kb_id=kb_id,
                ).single()
            else:
                rec = session.run(
                    """
                    MATCH (e:Entity)
                    OPTIONAL MATCH (e)-[r:RELATES_TO]->()
                    RETURN count(DISTINCT e) AS ec, count(DISTINCT r) AS rc
                    """
                ).single()
            return {
                "kb_id": kb_id or "all",
                "entity_count": int(rec["ec"]) if rec else 0,
                "relation_count": int(rec["rc"]) if rec else 0,
            }

    def find_relations_for_entities(
        self, kb_id: str, entity_names: list[str]
    ) -> list[dict[str, Any]]:
        """返回种子 + 双向邻居关系，字段对齐 KgRepository.find_relations_for_entities。

        输出行字段：
            entity_id / entity_name / entity_type / description / chunk_ids
            direction (self / source / target)
            rel_id / relation_type / rel_description / strength
            neighbor_id / neighbor_name / neighbor_type / neighbor_desc / neighbor_chunks
            source_name / target_name
        """
        if not entity_names:
            return []
        self.ensure_constraints()

        # 用三条独立 Cypher 拼三段（self / outgoing / incoming），UNION 合并
        # Neo4j 5+ Cypher UNION ALL 是允许的
        # 注意：每个 RETURN 必须字段顺序+数量完全一致
        cypher = """
        // seed 段：种子实体自身（无关系）
        MATCH (e:Entity {kb_id: $kb_id}) WHERE e.name IN $names
        RETURN elementId(e) AS entity_id, e.name AS entity_name,
               e.entity_type AS entity_type, e.description AS description,
               e.chunk_ids AS chunk_ids, 'self' AS direction,
               null AS rel_id, null AS relation_type,
               null AS rel_description, null AS strength,
               null AS neighbor_id, null AS neighbor_name,
               null AS neighbor_type, null AS neighbor_desc,
               null AS neighbor_chunks,
               null AS source_name, null AS target_name

        UNION ALL

        // outgoing：种子 e（source）→ 邻居 t（target）；主列输出邻居 t
        MATCH (e:Entity {kb_id: $kb_id})-[r:RELATES_TO {kb_id: $kb_id}]->(t:Entity)
        WHERE e.name IN $names
        RETURN elementId(t) AS entity_id, t.name AS entity_name,
               t.entity_type AS entity_type, t.description AS description,
               t.chunk_ids AS chunk_ids, 'source' AS direction,
               elementId(r) AS rel_id, r.relation_type AS relation_type,
               r.description AS rel_description, r.strength AS strength,
               elementId(e) AS neighbor_id, e.name AS neighbor_name,
               e.entity_type AS neighbor_type, e.description AS neighbor_desc,
               e.chunk_ids AS neighbor_chunks,
               e.name AS source_name, t.name AS target_name

        UNION ALL

        // incoming：种子 t（target）← 邻居 e（source）；主列输出邻居 e
        MATCH (e:Entity)-[r:RELATES_TO {kb_id: $kb_id}]->(t:Entity {kb_id: $kb_id})
        WHERE t.name IN $names
        RETURN elementId(e) AS entity_id, e.name AS entity_name,
               e.entity_type AS entity_type, e.description AS description,
               e.chunk_ids AS chunk_ids, 'target' AS direction,
               elementId(r) AS rel_id, r.relation_type AS relation_type,
               r.description AS rel_description, r.strength AS strength,
               elementId(t) AS neighbor_id, t.name AS neighbor_name,
               t.entity_type AS neighbor_type, t.description AS neighbor_desc,
               t.chunk_ids AS neighbor_chunks,
               e.name AS source_name, t.name AS target_name
        """
        with self._session() as session:
            result = session.run(cypher, kb_id=kb_id, names=entity_names)
            return [dict(record) for record in result]
