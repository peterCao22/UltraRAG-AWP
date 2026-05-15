"""Phase 6.2: KgStore.delete_by_doc (SQLite backend) 单测。

Neo4j 实现需要真服务连接，留到 manual G 段验证；这里只覆盖 SQLite。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir(exist_ok=True)
    from custom_app.repositories import set_default_provider

    set_default_provider(None)
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")
    monkeypatch.setenv("ULTRARAG_KG_BACKEND", "sqlite")
    yield tmp_path
    set_default_provider(None)


@pytest.fixture
def kg_store():
    from custom_app.db import init_db
    from custom_app.services.kgstore.base import build_kg_store

    init_db()
    return build_kg_store("sqlite")


def _seed_two_docs(kg_store, kb_id="kbX"):
    """构造两份文档的 KG：
      - doc A (stem="A"): 实体 e1 (chunks A_1, A_2) + 关系 r1
      - doc B (stem="B"): 实体 e2 (chunks B_1) + 关系 r2
      - 共享实体 e3：chunk_ids = [A_3, B_2]
    """
    e1 = kg_store.insert_entity(
        kb_id=kb_id, entity_name="EntA", entity_type="part",
        description="", chunk_ids_json=json.dumps(["A_1", "A_2"]),
        created_at="2026-05-15T00:00:00Z",
    )
    e2 = kg_store.insert_entity(
        kb_id=kb_id, entity_name="EntB", entity_type="part",
        description="", chunk_ids_json=json.dumps(["B_1"]),
        created_at="2026-05-15T00:00:00Z",
    )
    e3 = kg_store.insert_entity(
        kb_id=kb_id, entity_name="EntShared", entity_type="part",
        description="", chunk_ids_json=json.dumps(["A_3", "B_2"]),
        created_at="2026-05-15T00:00:00Z",
    )
    kg_store.insert_relation(
        kb_id=kb_id, source_id=e1, target_id=e3,
        relation_type="related", description="", strength=5,
        created_at="2026-05-15T00:00:00Z", doc_id="kbX:A.docx",
    )
    kg_store.insert_relation(
        kb_id=kb_id, source_id=e2, target_id=e3,
        relation_type="related", description="", strength=5,
        created_at="2026-05-15T00:00:00Z", doc_id="kbX:B.docx",
    )
    return {"e1": e1, "e2": e2, "e3": e3}


class TestDeleteByDoc:
    def test_removes_doc_specific_relation(self, kg_store):
        _seed_two_docs(kg_store)
        rel_del, ent_del = kg_store.delete_by_doc("kbX", "kbX:A.docx")
        # 删 r1（doc A 的关系）
        assert rel_del == 1
        # e1 整体属于 doc A（chunks A_1, A_2 都以 A_ 前缀） → 实体被删
        assert ent_del == 1

    def test_shared_entity_keeps_other_doc_chunks(self, kg_store):
        ids = _seed_two_docs(kg_store)
        kg_store.delete_by_doc("kbX", "kbX:A.docx")
        # e3 应保留，且 chunk_ids 应只剩 ["B_2"]
        from custom_app.repositories import KgRepository
        repo = KgRepository()
        rows = repo.list_entities_for_kb("kbX") if hasattr(repo, "list_entities_for_kb") else []
        # 用 find_entity_by_name 检查
        shared = kg_store.find_entity_by_name("kbX", "EntShared")
        assert shared is not None
        assert json.loads(shared.chunk_ids) == ["B_2"]

    def test_legacy_relation_without_doc_id_not_affected(self, kg_store):
        ids = _seed_two_docs(kg_store)
        # 再插一条 doc_id='' 的老关系
        kg_store.insert_relation(
            kb_id="kbX", source_id=ids["e2"], target_id=ids["e3"],
            relation_type="legacy", description="", strength=5,
            created_at="2026-05-15T00:00:00Z", doc_id="",
        )
        rel_del, _ = kg_store.delete_by_doc("kbX", "kbX:A.docx")
        # 只删 doc_id='kbX:A.docx' 的 1 条，legacy 不动
        assert rel_del == 1
        # 用 find_relation 验证 legacy 还在
        legacy = kg_store.find_relation(
            kb_id="kbX", source_id=str(ids["e2"]), target_id=str(ids["e3"]),
            relation_type="legacy",
        )
        assert legacy is not None

    def test_empty_doc_id_is_noop(self, kg_store):
        _seed_two_docs(kg_store)
        assert kg_store.delete_by_doc("kbX", "") == (0, 0)

    def test_unknown_doc_id_does_nothing(self, kg_store):
        _seed_two_docs(kg_store)
        assert kg_store.delete_by_doc("kbX", "kbX:missing.docx") == (0, 0)
