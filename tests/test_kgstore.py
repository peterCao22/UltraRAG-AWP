"""Phase 5.2 — KgStore Protocol 双后端测试。

测试策略：
    - Protocol/工厂层：纯单元（无外部依赖）
    - SqliteKgStore：用临时文件 SQLite 跑完整 CRUD（41 项 KG repo 已覆盖，这里只测包装层）
    - Neo4jKgStore：@pytest.mark.requires_neo4j，CI 默认 skip
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# 工厂 + Protocol
# ---------------------------------------------------------------------------


def test_resolve_kg_backend_env_default(monkeypatch):
    from custom_app.services.kgstore.base import resolve_kg_backend

    monkeypatch.delenv("ULTRARAG_KG_BACKEND", raising=False)
    assert resolve_kg_backend(None) == "sqlite"


def test_resolve_kg_backend_env_override(monkeypatch):
    from custom_app.services.kgstore.base import resolve_kg_backend

    monkeypatch.setenv("ULTRARAG_KG_BACKEND", "neo4j")
    assert resolve_kg_backend(None) == "neo4j"


def test_resolve_kg_backend_yaml_priority(monkeypatch):
    from custom_app.services.kgstore.base import resolve_kg_backend

    monkeypatch.setenv("ULTRARAG_KG_BACKEND", "neo4j")
    # YAML 显式给值优先级最高
    assert resolve_kg_backend("sqlite") == "sqlite"


def test_resolve_kg_backend_invalid(monkeypatch):
    from custom_app.services.kgstore.base import resolve_kg_backend

    monkeypatch.delenv("ULTRARAG_KG_BACKEND", raising=False)
    with pytest.raises(ValueError, match="invalid kg_backend"):
        resolve_kg_backend("mongo")


# ---------------------------------------------------------------------------
# SqliteKgStore（SQLite 后端，复用 KgRepository）
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_kg_env(tmp_path, monkeypatch):
    """临时文件型 SQLite + 重置 default provider。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    from custom_app.repositories import set_default_provider
    set_default_provider(None)
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")
    monkeypatch.setenv("ULTRARAG_KG_BACKEND", "sqlite")
    import custom_app.db as db_module
    db_module.init_db()
    yield tmp_path
    set_default_provider(None)


def test_sqlite_kg_store_satisfies_protocol(sqlite_kg_env):
    from custom_app.services.kgstore.base import KgStore
    from custom_app.services.kgstore.sqlite_store import SqliteKgStore

    store = SqliteKgStore()
    assert isinstance(store, KgStore)


def test_sqlite_kg_store_crud_lifecycle(sqlite_kg_env):
    from custom_app.services.kgstore.sqlite_store import SqliteKgStore

    store = SqliteKgStore()

    # find_entity_by_name 未存在
    assert store.find_entity_by_name("kb1", "电池") is None

    # insert
    eid_battery = store.insert_entity(
        kb_id="kb1", entity_name="电池", entity_type="Product",
        description="d", chunk_ids_json='["c1"]',
        created_at="2026-05-12T00:00:00Z",
    )
    assert isinstance(eid_battery, str)
    assert eid_battery != ""

    # find again
    found = store.find_entity_by_name("kb1", "电池")
    assert found is not None
    assert found.id == eid_battery
    assert found.chunk_ids == '["c1"]'

    # update_entity_full
    store.update_entity_full(
        eid_battery, entity_type="Product",
        description="updated", chunk_ids_json='["c1","c2"]',
    )
    found = store.find_entity_by_name("kb1", "电池")
    assert found.chunk_ids == '["c1","c2"]'

    # 第二个实体 + 关系
    eid_agv = store.insert_entity(
        kb_id="kb1", entity_name="AGV", entity_type="Product",
        description="", chunk_ids_json="[]",
        created_at="2026-05-12T00:00:00Z",
    )
    assert store.find_relation(
        kb_id="kb1", source_id=eid_battery, target_id=eid_agv,
        relation_type="part_of",
    ) is None
    store.insert_relation(
        kb_id="kb1", source_id=eid_battery, target_id=eid_agv,
        relation_type="part_of", description="电池是 AGV 部件",
        strength=8, created_at="2026-05-12T00:00:00Z",
    )
    assert store.find_relation(
        kb_id="kb1", source_id=eid_battery, target_id=eid_agv,
        relation_type="part_of",
    ) is not None

    # count
    stats = store.count_entities_and_relations("kb1")
    assert stats["entity_count"] == 2
    assert stats["relation_count"] == 1

    # find_relations_for_entities
    rows = store.find_relations_for_entities("kb1", ["电池"])
    directions = {r["direction"] for r in rows}
    assert "self" in directions
    assert "source" in directions  # 电池 →part_of→ AGV

    # delete
    rc, ec = store.delete_all_for_kb("kb1")
    assert (rc, ec) == (1, 2)
    assert store.find_entity_by_name("kb1", "电池") is None


# ---------------------------------------------------------------------------
# Neo4jKgStore Protocol 合规（不实际连接 Neo4j）
# ---------------------------------------------------------------------------


def test_neo4j_kg_store_class_satisfies_protocol_signature():
    """Neo4jKgStore 类应有 KgStore Protocol 要求的所有方法。"""
    pytest.importorskip("neo4j", reason="neo4j driver not installed")
    from custom_app.services.kgstore.neo4j_store import Neo4jKgStore
    for method in (
        "find_entity_by_name", "insert_entity", "update_entity_full",
        "find_relation", "insert_relation",
        "delete_all_for_kb", "count_entities_and_relations",
        "find_relations_for_entities",
    ):
        assert hasattr(Neo4jKgStore, method), f"Neo4jKgStore missing {method}"


def test_neo4j_kg_store_requires_uri(monkeypatch):
    pytest.importorskip("neo4j", reason="neo4j driver not installed")
    from custom_app.services.kgstore.neo4j_store import Neo4jKgStore

    monkeypatch.delenv("ULTRARAG_NEO4J_URI", raising=False)
    with pytest.raises(ValueError, match="ULTRARAG_NEO4J_URI"):
        Neo4jKgStore()


# ---------------------------------------------------------------------------
# 真实 Neo4j 集成（@pytest.mark.requires_neo4j）
# ---------------------------------------------------------------------------


@pytest.mark.requires_neo4j
def test_neo4j_kg_store_real_lifecycle():
    """完整 CRUD lifecycle 在真实 Neo4j 上跑通。"""
    from dotenv import load_dotenv
    load_dotenv()
    if not os.environ.get("ULTRARAG_NEO4J_URI"):
        pytest.skip("ULTRARAG_NEO4J_URI not set")

    from custom_app.services.kgstore.neo4j_store import Neo4jKgStore
    store = Neo4jKgStore()
    test_kb = f"pgtest_kg_{os.getpid()}"
    try:
        # 清理（幂等）
        store.delete_all_for_kb(test_kb)

        # CRUD
        eid_a = store.insert_entity(
            kb_id=test_kb, entity_name="A", entity_type="X",
            description="", chunk_ids_json="[]",
            created_at="2026-05-12T00:00:00Z",
        )
        eid_b = store.insert_entity(
            kb_id=test_kb, entity_name="B", entity_type="X",
            description="", chunk_ids_json="[]",
            created_at="2026-05-12T00:00:00Z",
        )
        assert store.find_entity_by_name(test_kb, "A").id == eid_a

        store.insert_relation(
            kb_id=test_kb, source_id=eid_a, target_id=eid_b,
            relation_type="r", description="", strength=5,
            created_at="2026-05-12T00:00:00Z",
        )
        assert store.find_relation(
            kb_id=test_kb, source_id=eid_a, target_id=eid_b, relation_type="r",
        ) is not None

        stats = store.count_entities_and_relations(test_kb)
        assert stats["entity_count"] == 2
        assert stats["relation_count"] == 1

        rows = store.find_relations_for_entities(test_kb, ["A"])
        directions = [r["direction"] for r in rows]
        assert "self" in directions
        assert "source" in directions

        rc, ec = store.delete_all_for_kb(test_kb)
        assert rc == 1 and ec == 2
    finally:
        # 兜底清理
        store.delete_all_for_kb(test_kb)
        store.close()


@pytest.mark.requires_neo4j
def test_neo4j_isolation_by_kb_id():
    """不同 kb_id 的实体/关系互不影响。"""
    from dotenv import load_dotenv
    load_dotenv()
    if not os.environ.get("ULTRARAG_NEO4J_URI"):
        pytest.skip("ULTRARAG_NEO4J_URI not set")

    from custom_app.services.kgstore.neo4j_store import Neo4jKgStore
    store = Neo4jKgStore()
    kb1 = f"pgtest_kg_iso1_{os.getpid()}"
    kb2 = f"pgtest_kg_iso2_{os.getpid()}"
    try:
        store.delete_all_for_kb(kb1)
        store.delete_all_for_kb(kb2)

        e1 = store.insert_entity(
            kb_id=kb1, entity_name="shared_name", entity_type="X",
            description="from kb1", chunk_ids_json="[]",
            created_at="2026-05-12T00:00:00Z",
        )
        e2 = store.insert_entity(
            kb_id=kb2, entity_name="shared_name", entity_type="X",
            description="from kb2", chunk_ids_json="[]",
            created_at="2026-05-12T00:00:00Z",
        )
        # 同名但不同 kb_id，element_id 必须不同
        assert e1 != e2
        # 各自只能找到自己 KB 的
        assert store.find_entity_by_name(kb1, "shared_name").id == e1
        assert store.find_entity_by_name(kb2, "shared_name").id == e2
        # delete kb1 不影响 kb2
        store.delete_all_for_kb(kb1)
        assert store.find_entity_by_name(kb1, "shared_name") is None
        assert store.find_entity_by_name(kb2, "shared_name") is not None
    finally:
        store.delete_all_for_kb(kb1)
        store.delete_all_for_kb(kb2)
        store.close()
