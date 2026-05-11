"""
知识图谱查询服务。

基于 SQLite 的 kg_entities / kg_relations 表进行图遍历查询。
模拟 Neo4j 的 MATCH (n)-[r]-(m) 模式。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from custom_app.db import get_conn

logger = logging.getLogger(__name__)


def search_graph(kb_id: str, entity_names: List[str],
                 max_depth: int = 1) -> dict[str, Any]:
    """查询知识图谱中的实体及其邻居关系。

    参数:
        kb_id: 知识库 ID
        entity_names: 要查询的实体名称列表
        max_depth: 查询深度（当前仅支持 1-hop）

    返回:
        {
            "entities": [{"id": int, "name": str, "type": str, "description": str, "chunk_ids": list}, ...],
            "relations": [{"id": int, "source": str, "target": str,
                          "relation_type": str, "description": str, "strength": int}, ...],
            "neighbor_entities": [...],  # 通过关系发现的邻居实体
            "all_chunk_ids": [...]  # 所有关联的 chunk ID
        }
    """
    if not entity_names:
        return {"entities": [], "relations": [], "neighbor_entities": [], "all_chunk_ids": []}

    placeholders = ",".join("?" for _ in entity_names)
    params = [kb_id] + entity_names

    # 1-hop 邻居查询：查找与输入实体有关系的节点
    # UNION 查询双向关系
    query = f"""
    SELECT e.id as entity_id, e.entity_name, e.entity_type, e.description,
           e.chunk_ids, 'self' as direction,
           NULL as rel_id, NULL as relation_type,
           NULL as rel_description, NULL as strength,
           NULL as neighbor_id, NULL as neighbor_name,
           NULL as neighbor_type, NULL as neighbor_desc,
           NULL as neighbor_chunks,
           NULL as source_name, NULL as target_name
    FROM kg_entities e
    WHERE e.kb_id = ? AND e.entity_name IN ({placeholders})

    UNION ALL

    -- outgoing 段：种子实体在 e（source）位置，邻居是 t（target）。
    -- 主列输出邻居 t 的字段，与 incoming 段保持一致："主列 = 邻居"。
    SELECT t.id as entity_id, t.entity_name, t.entity_type, t.description,
           t.chunk_ids, 'source' as direction,
           r.id as rel_id, r.relation_type, r.description as rel_description, r.strength,
           e.id as neighbor_id, e.entity_name as neighbor_name,
           e.entity_type as neighbor_type, e.description as neighbor_desc,
           e.chunk_ids as neighbor_chunks,
           e.entity_name as source_name, t.entity_name as target_name
    FROM kg_entities e
    JOIN kg_relations r ON r.source_id = e.id
    JOIN kg_entities t ON t.id = r.target_id
    WHERE e.kb_id = ? AND e.entity_name IN ({placeholders})

    UNION ALL

    -- incoming 段：种子实体在 t（target）位置，邻居是 e（source）。
    -- 主列输出邻居 e 的字段，与 outgoing 段保持一致："主列 = 邻居"。
    SELECT e.id as entity_id, e.entity_name, e.entity_type, e.description,
           e.chunk_ids, 'target' as direction,
           r.id as rel_id, r.relation_type, r.description as rel_description, r.strength,
           t.id as neighbor_id, t.entity_name as neighbor_name,
           t.entity_type as neighbor_type, t.description as neighbor_desc,
           t.chunk_ids as neighbor_chunks,
           e.entity_name as source_name, t.entity_name as target_name
    FROM kg_entities t
    JOIN kg_relations r ON r.target_id = t.id
    JOIN kg_entities e ON e.id = r.source_id
    WHERE t.kb_id = ? AND t.entity_name IN ({placeholders})
    """
    full_params = [kb_id] + entity_names + [kb_id] + entity_names + [kb_id] + entity_names

    rows: list[dict] = []
    with get_conn() as conn:
        rows = [dict(row) for row in conn.execute(query, full_params).fetchall()]

    # 去重处理
    seen_entities: Dict[str, dict] = {}
    seen_relations: Dict[int, dict] = {}
    all_chunk_ids: Set[str] = set()

    # 先收集种子实体
    for row in rows:
        if row["direction"] != "self":
            continue
        eid = row["entity_id"]
        ename = row["entity_name"]
        try:
            chunk_ids = json.loads(row["chunk_ids"] or "[]")
        except (json.JSONDecodeError, TypeError):
            chunk_ids = []
        all_chunk_ids.update(chunk_ids)
        seen_entities[ename] = {
            "id": eid,
            "name": ename,
            "type": row["entity_type"],
            "description": row["description"] or "",
            "chunk_ids": chunk_ids,
        }

    # 再处理关系和邻居实体
    for row in rows:
        if row["direction"] == "self":
            continue
        eid = row["entity_id"]
        ename = row["entity_name"]
        direction = row["direction"]

        # 解析 chunk_ids
        try:
            chunk_ids = json.loads(row["chunk_ids"] or "[]")
        except (json.JSONDecodeError, TypeError):
            chunk_ids = []
        all_chunk_ids.update(chunk_ids)

        # 实体去重
        if ename not in seen_entities:
            seen_entities[ename] = {
                "id": eid,
                "name": ename,
                "type": row["entity_type"],
                "description": row["description"] or "",
                "chunk_ids": chunk_ids,
            }

        # 关系去重（用 rel_id 去重，source 和 target 从原始关系记录取）
        rel_id = row["rel_id"]
        if rel_id and rel_id not in seen_relations:
            seen_relations[rel_id] = {
                "id": rel_id,
                "source": row["source_name"],
                "target": row["target_name"],
                "relation_type": row["relation_type"] or "",
                "description": row["rel_description"] or "",
                "strength": row["strength"] or 5,
            }

    # 分离种子实体和邻居实体
    seed_names = {n for n in entity_names}
    entities = [v for k, v in seen_entities.items() if k in seed_names]
    neighbor_entities = [v for k, v in seen_entities.items() if k not in seed_names]

    return {
        "entities": entities,
        "relations": list(seen_relations.values()),
        "neighbor_entities": neighbor_entities,
        "all_chunk_ids": list(all_chunk_ids),
    }


def collect_chunk_ids(kb_id: str, entity_names: List[str]) -> List[str]:
    """从图谱结果中提取所有关联的 chunk_id。

    用于增强 RagRunner 的检索上下文。
    """
    result = search_graph(kb_id, entity_names)
    return result["all_chunk_ids"]


def get_graph_stats(kb_id: Optional[str] = None) -> dict:
    """获取图谱统计信息。

    参数:
        kb_id: 可选，指定 KB 的统计；None 则返回全局统计

    返回:
        {"kb_id": str, "entity_count": int, "relation_count": int}
    """
    with get_conn() as conn:
        if kb_id:
            row = conn.execute(
                "SELECT COUNT(DISTINCT e.id) as ec, COUNT(DISTINCT r.id) as rc "
                "FROM kg_entities e LEFT JOIN kg_relations r ON r.source_id = e.id "
                "WHERE e.kb_id = ?",
                (kb_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(DISTINCT e.id) as ec, COUNT(DISTINCT r.id) as rc "
                "FROM kg_entities e LEFT JOIN kg_relations r ON r.source_id = e.id"
            ).fetchone()

    return {
        "kb_id": kb_id or "all",
        "entity_count": row["ec"] if row else 0,
        "relation_count": row["rc"] if row else 0,
    }


def clear_graph(kb_id: str) -> int:
    """清除指定 KB 的图谱数据。

    返回删除的关系记录数。
    """
    with get_conn() as conn:
        rel_count = conn.execute(
            "SELECT COUNT(*) FROM kg_relations WHERE kb_id=?", (kb_id,)
        ).fetchone()[0]
        conn.execute("DELETE FROM kg_relations WHERE kb_id=?", (kb_id,))
        conn.execute("DELETE FROM kg_entities WHERE kb_id=?", (kb_id,))
    return rel_count
