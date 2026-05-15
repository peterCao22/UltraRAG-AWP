"""SqliteKgStore —— KgStore Protocol 的 SQLite 实现。

通过包装现有 KgRepository 复用 Phase 5.1 的 SQL 代码；
entity_id 在 Protocol 层是 str，内部转换为 int 给 KgRepository 用。
"""

from __future__ import annotations

from typing import Any, Optional

from custom_app.repositories import KgRepository
from custom_app.services.kgstore.base import EntityRecord


class SqliteKgStore:
    """SQLite 后端：KgRepository 的薄包装层。"""

    def __init__(self) -> None:
        self._repo = KgRepository()

    def find_entity_by_name(
        self, kb_id: str, entity_name: str
    ) -> Optional[EntityRecord]:
        row = self._repo.find_entity_by_name(kb_id, entity_name)
        if row is None:
            return None
        return EntityRecord(
            id=str(row["id"]),
            chunk_ids=row.get("chunk_ids") or "[]",
        )

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
        eid = self._repo.insert_entity(
            kb_id=kb_id,
            entity_name=entity_name,
            entity_type=entity_type,
            description=description,
            chunk_ids_json=chunk_ids_json,
            created_at=created_at,
        )
        return str(eid)

    def update_entity_full(
        self,
        entity_id: str,
        *,
        entity_type: str,
        description: str,
        chunk_ids_json: str,
    ) -> None:
        self._repo.update_entity_full(
            int(entity_id),
            entity_type=entity_type,
            description=description,
            chunk_ids_json=chunk_ids_json,
        )

    def find_relation(
        self,
        *,
        kb_id: str,
        source_id: str,
        target_id: str,
        relation_type: str,
    ) -> Optional[dict[str, Any]]:
        return self._repo.find_relation(
            kb_id=kb_id,
            source_id=int(source_id),
            target_id=int(target_id),
            relation_type=relation_type,
        )

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
        self._repo.insert_relation(
            kb_id=kb_id,
            source_id=int(source_id),
            target_id=int(target_id),
            relation_type=relation_type,
            description=description,
            strength=strength,
            created_at=created_at,
            doc_id=doc_id,
        )

    def delete_all_for_kb(self, kb_id: str) -> tuple[int, int]:
        return self._repo.delete_all_for_kb(kb_id)

    def delete_by_doc(self, kb_id: str, doc_id: str) -> tuple[int, int]:
        """Phase 6.2: 委托给 KgRepository.delete_by_doc。

        KgRepository 需要 doc_stem 来匹配实体的 chunk_ids 前缀；这里从 doc_id 推导。
        """
        from custom_app.utils.chunks_io import doc_id_to_stem

        return self._repo.delete_by_doc(kb_id, doc_id, doc_id_to_stem(doc_id))

    def count_entities_and_relations(
        self, kb_id: Optional[str] = None
    ) -> dict[str, Any]:
        return self._repo.count_entities_and_relations(kb_id)

    def find_relations_for_entities(
        self, kb_id: str, entity_names: list[str]
    ) -> list[dict[str, Any]]:
        return self._repo.find_relations_for_entities(kb_id, entity_names)
