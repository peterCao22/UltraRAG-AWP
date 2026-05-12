"""
Hotfix TDD：kg_search.search_graph 的 incoming 关系分支 SQL bug

修复前：第三段 UNION (incoming relations) 写错了过滤条件，
       WHERE t.kb_id = ? AND e.entity_name IN (...)  ← 应该是 t.entity_name
       导致所有 "邻居 → 种子实体" 方向的关系都查不到，
       且即便没有这个 bug，主列输出的也是种子自己（不是邻居），
       Python 处理时不会把邻居加到 seen_entities。

修复后：incoming 段返回邻居 e 的字段做主列，过滤 t.entity_name 匹配种子，
       与 outgoing 段保持 "主列 = 邻居" 的语义。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def mem_db(monkeypatch, tmp_path):
    """Phase 5.1.7：临时文件型 SQLite + 默认 sqlite provider。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    from custom_app.repositories import set_default_provider
    set_default_provider(None)
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")
    import custom_app.db as db_module
    db_module.init_db()
    conn = sqlite3.connect(tmp_path / "db" / "app.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()
    set_default_provider(None)


def _seed(conn, kb_id: str, entities: list[dict], relations: list[dict]) -> dict[str, int]:
    """插入实体和关系，返回 name → id 的映射。"""
    from custom_app.db import now_iso
    ts = now_iso()
    name_to_id: dict[str, int] = {}
    for e in entities:
        cur = conn.execute(
            "INSERT INTO kg_entities (kb_id, entity_name, entity_type, description, chunk_ids, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (kb_id, e["name"], e["type"], e.get("desc", ""),
             json.dumps(e.get("chunks", [])), ts),
        )
        name_to_id[e["name"]] = cur.lastrowid
    for r in relations:
        conn.execute(
            "INSERT INTO kg_relations (kb_id, source_id, target_id, relation_type, description, strength, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kb_id, name_to_id[r["src"]], name_to_id[r["tgt"]],
             r["rel"], r.get("desc", ""), r.get("strength", 5), ts),
        )
    conn.commit()  # Phase 5.1.7：Repository 用独立连接读，需 commit
    return name_to_id


class TestIncomingRelations:
    """模拟 IFS 数据：'Issue Type页签 →[configures]→ 出库类型' 这种从邻居指向种子的关系。"""

    def test_incoming_relation_returned(self, mem_db):
        from custom_app.services.kg_search import search_graph
        _seed(mem_db, "kb1",
              entities=[
                  {"name": "出库类型", "type": "Category", "chunks": ["c1"]},
                  {"name": "Issue Type页签", "type": "Resource", "chunks": ["c1"]},
              ],
              relations=[
                  {"src": "Issue Type页签", "tgt": "出库类型", "rel": "configures"},
              ])

        result = search_graph("kb1", ["出库类型"])
        assert len(result["entities"]) == 1
        assert result["entities"][0]["name"] == "出库类型"
        assert len(result["relations"]) == 1, "incoming 关系应被找到"
        rel = result["relations"][0]
        assert rel["source"] == "Issue Type页签"
        assert rel["target"] == "出库类型"
        assert rel["relation_type"] == "configures"

    def test_incoming_neighbor_listed(self, mem_db):
        """incoming 关系的邻居（source 端实体）应在 neighbor_entities。"""
        from custom_app.services.kg_search import search_graph
        _seed(mem_db, "kb1",
              entities=[
                  {"name": "零件状态", "type": "Concept", "chunks": ["c1"]},
                  {"name": "On Hand Qty", "type": "Resource", "chunks": ["c1"]},
                  {"name": "Demand", "type": "Resource", "chunks": ["c1"]},
              ],
              relations=[
                  {"src": "On Hand Qty", "tgt": "零件状态", "rel": "defines_attribute_of"},
                  {"src": "Demand", "tgt": "零件状态", "rel": "defines_attribute_of"},
              ])

        result = search_graph("kb1", ["零件状态"])
        names = {e["name"] for e in result["neighbor_entities"]}
        assert "On Hand Qty" in names
        assert "Demand" in names
        # 种子本身不应出现在 neighbors
        assert "零件状态" not in names

    def test_outgoing_still_works(self, mem_db):
        """修复 incoming 不应破坏原本工作的 outgoing。"""
        from custom_app.services.kg_search import search_graph
        _seed(mem_db, "kb1",
              entities=[
                  {"name": "种子A", "type": "Concept"},
                  {"name": "下游B", "type": "Resource"},
              ],
              relations=[
                  {"src": "种子A", "tgt": "下游B", "rel": "uses"},
              ])
        result = search_graph("kb1", ["种子A"])
        assert len(result["relations"]) == 1
        assert result["relations"][0]["source"] == "种子A"
        assert result["relations"][0]["target"] == "下游B"
        names = {e["name"] for e in result["neighbor_entities"]}
        assert "下游B" in names

    def test_bidirectional_relation_no_duplicate(self, mem_db):
        """A→B 和 B→A 是两条独立关系，搜索 A 应只返回 A 出/入的关系，不重复。"""
        from custom_app.services.kg_search import search_graph
        _seed(mem_db, "kb1",
              entities=[
                  {"name": "A", "type": "Concept"},
                  {"name": "B", "type": "Concept"},
              ],
              relations=[
                  {"src": "A", "tgt": "B", "rel": "calls"},
                  {"src": "B", "tgt": "A", "rel": "responds_to"},
              ])
        result = search_graph("kb1", ["A"])
        assert len(result["relations"]) == 2
        rel_types = sorted(r["relation_type"] for r in result["relations"])
        assert rel_types == ["calls", "responds_to"]

    def test_incoming_chunk_ids_collected(self, mem_db):
        """incoming 邻居的 chunk_ids 应被并入 all_chunk_ids，供 LLM 进一步阅读。"""
        from custom_app.services.kg_search import search_graph
        _seed(mem_db, "kb1",
              entities=[
                  {"name": "种子", "type": "Concept", "chunks": ["c_seed"]},
                  {"name": "邻居", "type": "Resource", "chunks": ["c_neighbor"]},
              ],
              relations=[
                  {"src": "邻居", "tgt": "种子", "rel": "configures"},
              ])
        result = search_graph("kb1", ["种子"])
        assert "c_seed" in result["all_chunk_ids"]
        assert "c_neighbor" in result["all_chunk_ids"]

    def test_no_match_returns_empty(self, mem_db):
        from custom_app.services.kg_search import search_graph
        _seed(mem_db, "kb1",
              entities=[{"name": "X", "type": "Concept"}],
              relations=[])
        result = search_graph("kb1", ["不存在"])
        assert result["entities"] == []
        assert result["relations"] == []
        assert result["neighbor_entities"] == []
