"""KgStore Protocol —— Phase 5.2 知识图谱存储统一接口。

为什么需要这层抽象：
    Phase 5.1 把 KG 数据访问集中到 KgRepository（SQL Repository），
    但 Neo4j 不是 SQL；用 Cypher / driver session 而非 placeholder SQL。
    用 KgStore Protocol 让 kg_extractor / kg_search 不感知后端差异。

设计要点：
    - entity_id 用 str 而非 int（SQLite int 转 str，Neo4j 用 element_id 字符串）
    - find_relations_for_entities 返回的 row 含 direction（self/source/target）
      和邻居信息，与 KgRepository 现有契约一致
    - delete_all_for_kb 返回 (rel_count, ent_count) 与 Repository 一致
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntityRecord:
    """实体记录（KgStore 返回值）。

    id 字段统一为 str：SQLite 后端把 int 自动转 str；
    Neo4j 后端直接用 element_id。上层比较时用 str。
    """

    id: str
    chunk_ids: str  # 原始 JSON 字符串，调用方负责 json.loads


@runtime_checkable
class KgStore(Protocol):
    """知识图谱存储 Protocol（与现有 KgRepository 签名兼容）。

    实现要点：
        1. 所有方法异常抛出，不静默返回
        2. delete_all_for_kb 返回 (rel_count, ent_count) tuple
        3. find_relations_for_entities 必须返回含 direction 字段的字典
    """

    def find_entity_by_name(
        self, kb_id: str, entity_name: str
    ) -> Optional[EntityRecord]:
        ...

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
        """返回新实体 ID（str）。"""
        ...

    def update_entity_full(
        self,
        entity_id: str,
        *,
        entity_type: str,
        description: str,
        chunk_ids_json: str,
    ) -> None:
        ...

    def find_relation(
        self,
        *,
        kb_id: str,
        source_id: str,
        target_id: str,
        relation_type: str,
    ) -> Optional[dict[str, Any]]:
        ...

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
        """Phase 6.2: doc_id 标记关系来源文档，便于按 doc 删除；老调用可省略。"""
        ...

    def delete_all_for_kb(self, kb_id: str) -> tuple[int, int]:
        """删除某 KB 下所有实体+关系；返回 (relation_count, entity_count)。"""
        ...

    def delete_by_doc(self, kb_id: str, doc_id: str) -> tuple[int, int]:
        """Phase 6.2: 删除某 doc 的 KG 数据。

        实现要点：
            1. 删除该 doc 的所有 relation（按 (kb_id, doc_id) 过滤）
            2. 实体的 chunk_ids 移除该 doc 的 chunk ids；chunk_ids 为空时实体也删
        返回 (relations_deleted, entities_deleted)。

        老数据兼容：doc_id 为空字符串的旧关系/实体不会被这个方法影响。
        """
        ...

    def count_entities_and_relations(
        self, kb_id: Optional[str] = None
    ) -> dict[str, Any]:
        ...

    def find_relations_for_entities(
        self, kb_id: str, entity_names: list[str]
    ) -> list[dict[str, Any]]:
        """返回种子 + 双向邻居关系；含 direction / source_name / target_name 等字段。"""
        ...


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


VALID_KG_BACKENDS = frozenset({"sqlite", "neo4j"})


def resolve_kg_backend(yaml_value: Optional[str] = None) -> str:
    """优先级：YAML > env ULTRARAG_KG_BACKEND > 默认 sqlite。"""
    backend = (
        (yaml_value or "").strip()
        or os.environ.get("ULTRARAG_KG_BACKEND", "").strip()
        or "sqlite"
    ).lower()
    if backend not in VALID_KG_BACKENDS:
        raise ValueError(
            f"invalid kg_backend {backend!r}, expected one of {sorted(VALID_KG_BACKENDS)}"
        )
    return backend


def build_kg_store(backend: Optional[str] = None) -> KgStore:
    """构造 KgStore 实例。

    Args:
        backend: 显式指定后端；None 时按 resolve_kg_backend() 决定
    """
    resolved = resolve_kg_backend(backend)
    if resolved == "sqlite":
        from custom_app.services.kgstore.sqlite_store import SqliteKgStore
        return SqliteKgStore()
    if resolved == "neo4j":
        from custom_app.services.kgstore.neo4j_store import Neo4jKgStore
        return Neo4jKgStore()
    raise ValueError(f"unhandled backend {resolved!r}")  # unreachable
