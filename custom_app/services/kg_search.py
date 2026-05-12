"""
知识图谱查询服务。

基于 SQLite 的 kg_entities / kg_relations 表进行图遍历查询。
模拟 Neo4j 的 MATCH (n)-[r]-(m) 模式。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from custom_app.services.kgstore import build_kg_store

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

    rows = build_kg_store().find_relations_for_entities(kb_id, entity_names)

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
    return build_kg_store().count_entities_and_relations(kb_id)


def clear_graph(kb_id: str) -> int:
    """清除指定 KB 的图谱数据。

    返回删除的关系记录数。
    """
    rel_count, _ = build_kg_store().delete_all_for_kb(kb_id)
    return rel_count
