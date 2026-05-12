"""Phase 5.2 知识图谱存储包：KgStore Protocol + 具体实现。

模块结构：
    base            —— KgStore Protocol + EntityRecord / RelationRow dataclasses
    sqlite_store    —— SqliteKgStore（包装 KgRepository）
    neo4j_store     —— Neo4jKgStore（节点 :Entity {kb_id, name, ...}）

切换：
    - YAML / env ULTRARAG_KG_BACKEND=sqlite|neo4j
    - resolve_kg_backend() + build_kg_store() 工厂
"""

from custom_app.services.kgstore.base import (
    VALID_KG_BACKENDS,
    EntityRecord,
    KgStore,
    build_kg_store,
    resolve_kg_backend,
)

__all__ = [
    "EntityRecord",
    "KgStore",
    "VALID_KG_BACKENDS",
    "build_kg_store",
    "resolve_kg_backend",
]
