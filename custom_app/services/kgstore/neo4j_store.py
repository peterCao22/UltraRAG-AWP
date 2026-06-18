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
            # Phase 9.3：Image 节点约束（kb_id, img_id 联合唯一）+ kb_id 索引
            session.run(
                """
                CREATE CONSTRAINT image_kb_img_unique IF NOT EXISTS
                FOR (i:Image) REQUIRE (i.kb_id, i.img_id) IS UNIQUE
                """
            )
            session.run(
                "CREATE INDEX image_kb_id IF NOT EXISTS FOR (i:Image) ON (i.kb_id)"
            )
            session.run(
                "CREATE INDEX mentions_kb_id IF NOT EXISTS "
                "FOR ()-[r:MENTIONS]-() ON (r.kb_id)"
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
            # 1) 删关系并统计：一条语句 DELETE + RETURN count
            rel_rec = session.run(
                """
                MATCH ()-[r:RELATES_TO {kb_id: $kb_id, doc_id: $doc_id}]->()
                DELETE r
                RETURN count(r) AS cnt
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

    # ------------------------------------------------------------------
    # Phase 9.3：Image 节点 + MENTIONS 关系
    # ------------------------------------------------------------------

    def upsert_image_node(
        self,
        *,
        kb_id: str,
        img_id: str,
        path: str,
        doc: str,
        chunk_id: str,
        caption_zh: str,
        caption_en: str,
        created_at: str,
    ) -> str:
        """幂等创建或更新 :Image 节点。返回 Neo4j elementId。

        约束 (kb_id, img_id) 唯一；同 img_id 重复调用会更新 caption 等字段。
        """
        self.ensure_constraints()
        with self._session() as session:
            rec = session.run(
                """
                MERGE (i:Image {kb_id: $kb_id, img_id: $img_id})
                ON CREATE SET
                  i.path = $path,
                  i.doc = $doc,
                  i.chunk_id = $chunk_id,
                  i.caption_zh = $caption_zh,
                  i.caption_en = $caption_en,
                  i.created_at = $created_at
                ON MATCH SET
                  i.path = $path,
                  i.doc = $doc,
                  i.chunk_id = $chunk_id,
                  i.caption_zh = $caption_zh,
                  i.caption_en = $caption_en
                RETURN elementId(i) AS id
                """,
                kb_id=kb_id, img_id=img_id, path=path, doc=doc,
                chunk_id=chunk_id, caption_zh=caption_zh,
                caption_en=caption_en, created_at=created_at,
            ).single()
            return str(rec["id"])

    def link_image_to_entity(
        self,
        *,
        kb_id: str,
        img_id: str,
        entity_name: str,
        created_at: str,
    ) -> bool:
        """建 (:Image)-[:MENTIONS]->(:Entity) 关系；Entity 不存在时跳过返 False。

        kb_id 严格隔离：MENTIONS 关系 property 含 kb_id；Image 和 Entity 必须
        是同 kb_id 才连接。
        """
        self.ensure_constraints()
        with self._session() as session:
            rec = session.run(
                """
                MATCH (i:Image {kb_id: $kb_id, img_id: $img_id})
                MATCH (e:Entity {kb_id: $kb_id, name: $entity_name})
                MERGE (i)-[r:MENTIONS {kb_id: $kb_id}]->(e)
                ON CREATE SET r.created_at = $created_at
                RETURN elementId(r) AS rel_id
                """,
                kb_id=kb_id, img_id=img_id, entity_name=entity_name,
                created_at=created_at,
            ).single()
            return rec is not None

    def list_entity_chunk_id_counts(
        self, kb_id: str,
    ) -> dict[str, int]:
        """返回该 KB 下每个实体名 → chunk_ids 数组长度。Phase 9.3 过滤通用词用。

        实体 chunk_ids 是 JSON 字符串，这里逐条解析；KG 通常 < 300 实体，
        全表扫成本可接受。
        """
        if not kb_id:
            return {}
        import json as _json
        self.ensure_constraints()
        with self._session() as session:
            recs = session.run(
                "MATCH (e:Entity {kb_id: $kb_id}) "
                "RETURN e.name AS name, e.chunk_ids AS chunk_ids",
                kb_id=kb_id,
            ).data()
        out: dict[str, int] = {}
        for r in recs:
            name = str(r.get("name") or "")
            if not name:
                continue
            raw = r.get("chunk_ids") or "[]"
            try:
                arr = _json.loads(raw)
                cnt = len(arr) if isinstance(arr, list) else 0
            except (ValueError, TypeError):
                cnt = 0
            out[name] = cnt
        return out

    def list_entity_names_for_chunks(
        self, kb_id: str, chunk_ids: list[str],
    ) -> list[str]:
        """Phase 9.3.B：给定一组 chunk_id，返回所有出现在其中的实体名（去重）。

        实现：扫所有实体的 chunk_ids JSON 数组，O(N_entities) Python 端过滤。
        KG 通常 < 500 实体；远比每个 chunk 单独 Cypher 查快。
        """
        if not kb_id or not chunk_ids:
            return []
        import json as _json
        target = set(str(c) for c in chunk_ids)
        self.ensure_constraints()
        with self._session() as session:
            recs = session.run(
                "MATCH (e:Entity {kb_id: $kb_id}) "
                "RETURN e.name AS name, e.chunk_ids AS chunk_ids",
                kb_id=kb_id,
            ).data()
        out: list[str] = []
        seen: set[str] = set()
        for r in recs:
            name = str(r.get("name") or "")
            if not name or name in seen:
                continue
            raw = r.get("chunk_ids") or "[]"
            try:
                arr = _json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(arr, list):
                continue
            if any(str(c) in target for c in arr):
                out.append(name)
                seen.add(name)
        return out

    def count_images(self, kb_id: Optional[str] = None) -> dict[str, int]:
        """统计图节点 + MENTIONS 关系数。"""
        self.ensure_constraints()
        with self._session() as session:
            if kb_id:
                rec = session.run(
                    """
                    MATCH (i:Image {kb_id: $kb_id})
                    OPTIONAL MATCH (i)-[m:MENTIONS]->()
                    WHERE m.kb_id = $kb_id
                    RETURN count(DISTINCT i) AS ic, count(DISTINCT m) AS mc
                    """,
                    kb_id=kb_id,
                ).single()
            else:
                rec = session.run(
                    """
                    MATCH (i:Image)
                    OPTIONAL MATCH (i)-[m:MENTIONS]->()
                    RETURN count(DISTINCT i) AS ic, count(DISTINCT m) AS mc
                    """
                ).single()
            return {
                "image_count": int(rec["ic"]) if rec else 0,
                "mentions_count": int(rec["mc"]) if rec else 0,
            }

    def delete_images_for_kb(self, kb_id: str) -> int:
        """删除某 KB 下所有 :Image 节点（DETACH 自动级联删 MENTIONS）。"""
        self.ensure_constraints()
        with self._session() as session:
            rec = session.run(
                """
                MATCH (i:Image {kb_id: $kb_id})
                WITH count(i) AS n
                MATCH (i:Image {kb_id: $kb_id})
                DETACH DELETE i
                RETURN n
                """,
                kb_id=kb_id,
            ).single()
            return int(rec["n"]) if rec else 0

    def find_images_for_entities(
        self,
        kb_id: str,
        entity_names: list[str],
        *,
        exclude_chunk_ids: Optional[list[str]] = None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Phase 9.3.B 检索路径：给定一组实体名，找它们 MENTIONS 关联的图片。

        参数:
            kb_id:             KB 隔离
            entity_names:      种子实体列表（来自命中 chunk 的实体）
            exclude_chunk_ids: 排除已经在命中 chunk 列表里的图片（避免重复）；
                               传入命中 chunk_id 列表即可
            limit:             最多返回几张图（默认 3，env 可调）

        返回:
            按"被多少实体提及"降序排列 + 限 limit 张图：
            [{img_id, path, doc, chunk_id, caption_zh, caption_en, hit_count}, ...]
        """
        if not entity_names or not kb_id:
            return []
        self.ensure_constraints()
        exclude = set(str(c) for c in (exclude_chunk_ids or []))
        with self._session() as session:
            recs = session.run(
                """
                MATCH (e:Entity {kb_id: $kb_id})
                WHERE e.name IN $entity_names
                MATCH (i:Image {kb_id: $kb_id})-[:MENTIONS {kb_id: $kb_id}]->(e)
                WITH i, count(DISTINCT e) AS hits
                RETURN i.img_id AS img_id, i.path AS path, i.doc AS doc,
                       i.chunk_id AS chunk_id, i.caption_zh AS caption_zh,
                       i.caption_en AS caption_en, hits
                ORDER BY hits DESC, i.img_id ASC
                LIMIT $limit_cap
                """,
                kb_id=kb_id, entity_names=entity_names,
                # 多读几条，让上层按 exclude 过滤后还能凑齐 limit
                limit_cap=max(int(limit) * 3, int(limit)),
            ).data()
        out: list[dict[str, Any]] = []
        for r in recs:
            if str(r.get("chunk_id") or "") in exclude:
                continue
            out.append({
                "img_id": str(r.get("img_id") or ""),
                "path": str(r.get("path") or ""),
                "doc": str(r.get("doc") or ""),
                "chunk_id": str(r.get("chunk_id") or ""),
                "caption_zh": str(r.get("caption_zh") or ""),
                "caption_en": str(r.get("caption_en") or ""),
                "hit_count": int(r.get("hits") or 0),
            })
            if len(out) >= int(limit):
                break
        return out

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
