"""Phase 9.3.A Image 节点 + MENTIONS 关系单元测试。

完全 mock Neo4j driver / session，不依赖远程服务，验证：
  - upsert_image_node Cypher 参数正确
  - link_image_to_entity Entity 不存在时返回 False
  - count_images / delete_images_for_kb 接口契约
  - find_images_for_entities 排序 + exclude + limit 行为
  - list_entity_chunk_id_counts 解析 JSON
"""

from __future__ import annotations

import json as _json
from typing import Any
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# fixture：mock 一个 Neo4jKgStore，session 走 MagicMock
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_neo4j_store(monkeypatch):
    """构造一个 Neo4jKgStore，避免真连 driver。session() 返回 MagicMock。"""
    monkeypatch.setenv("ULTRARAG_NEO4J_URI", "bolt://fake:7687")
    monkeypatch.setenv("ULTRARAG_NEO4J_PASSWORD", "fake")

    from custom_app.services.kgstore.neo4j_store import Neo4jKgStore

    # 不让真连 driver
    monkeypatch.setattr(
        Neo4jKgStore, "_build_driver", lambda self: MagicMock(name="driver"),
    )
    store = Neo4jKgStore()
    # 跳过 ensure_constraints 的真实运行
    store._ensured_schema = True
    return store


@pytest.fixture()
def session_ctx():
    """返回一个 MagicMock session 上下文管理器 + 它内部的 run mock。

    用法：把 store._session 替换成返回这个 ctx；测试再断言 ctx.run.call_args
    """
    ctx_mgr = MagicMock(name="session_ctx_mgr")
    ses = MagicMock(name="session")
    ctx_mgr.__enter__ = MagicMock(return_value=ses)
    ctx_mgr.__exit__ = MagicMock(return_value=False)
    return ctx_mgr, ses


# ---------------------------------------------------------------------------
# upsert_image_node
# ---------------------------------------------------------------------------


def test_upsert_image_node_returns_element_id(mock_neo4j_store, session_ctx):
    ctx, ses = session_ctx
    ses.run.return_value.single.return_value = {"id": "image_elem_123"}
    mock_neo4j_store._session = lambda: ctx

    eid = mock_neo4j_store.upsert_image_node(
        kb_id="agv_demo", img_id="abc12345",
        path="images/x.jpg", doc="DocA", chunk_id="DocA_step_1",
        caption_zh="z", caption_en="e", created_at="2026-06-10T00:00:00Z",
    )

    assert eid == "image_elem_123"
    # 验证 MERGE Cypher 含关键参数
    call = ses.run.call_args
    cypher = call[0][0]
    assert "MERGE (i:Image" in cypher
    kwargs = call[1]
    assert kwargs["kb_id"] == "agv_demo"
    assert kwargs["img_id"] == "abc12345"
    assert kwargs["path"] == "images/x.jpg"
    assert kwargs["caption_zh"] == "z"


# ---------------------------------------------------------------------------
# link_image_to_entity
# ---------------------------------------------------------------------------


def test_link_image_to_entity_success(mock_neo4j_store, session_ctx):
    ctx, ses = session_ctx
    ses.run.return_value.single.return_value = {"rel_id": "rel_99"}
    mock_neo4j_store._session = lambda: ctx

    ok = mock_neo4j_store.link_image_to_entity(
        kb_id="agv_demo", img_id="abc12345", entity_name="急停按钮",
        created_at="2026-06-10T00:00:00Z",
    )

    assert ok is True
    call = ses.run.call_args
    cypher = call[0][0]
    # 严格匹配：MATCH Entity + MERGE MENTIONS
    assert "MATCH (i:Image" in cypher
    assert "MATCH (e:Entity" in cypher
    assert "MERGE (i)-[r:MENTIONS" in cypher
    kwargs = call[1]
    assert kwargs["entity_name"] == "急停按钮"
    assert kwargs["kb_id"] == "agv_demo"


def test_link_image_to_entity_entity_missing_returns_false(mock_neo4j_store, session_ctx):
    """Entity 不存在时 MATCH 返 None；接口应返 False，不报错。"""
    ctx, ses = session_ctx
    ses.run.return_value.single.return_value = None
    mock_neo4j_store._session = lambda: ctx

    ok = mock_neo4j_store.link_image_to_entity(
        kb_id="agv_demo", img_id="abc12345", entity_name="不存在的实体",
        created_at="t",
    )
    assert ok is False


# ---------------------------------------------------------------------------
# count_images
# ---------------------------------------------------------------------------


def test_count_images_by_kb(mock_neo4j_store, session_ctx):
    ctx, ses = session_ctx
    ses.run.return_value.single.return_value = {"ic": 17, "mc": 42}
    mock_neo4j_store._session = lambda: ctx

    out = mock_neo4j_store.count_images(kb_id="agv_demo")
    assert out == {"image_count": 17, "mentions_count": 42}
    cypher = ses.run.call_args[0][0]
    assert "MATCH (i:Image {kb_id: $kb_id})" in cypher


def test_count_images_all_kbs(mock_neo4j_store, session_ctx):
    ctx, ses = session_ctx
    ses.run.return_value.single.return_value = {"ic": 50, "mc": 100}
    mock_neo4j_store._session = lambda: ctx

    out = mock_neo4j_store.count_images()
    assert out == {"image_count": 50, "mentions_count": 100}


# ---------------------------------------------------------------------------
# delete_images_for_kb
# ---------------------------------------------------------------------------


def test_delete_images_for_kb(mock_neo4j_store, session_ctx):
    ctx, ses = session_ctx
    ses.run.return_value.single.return_value = {"n": 7}
    mock_neo4j_store._session = lambda: ctx

    n = mock_neo4j_store.delete_images_for_kb("agv_demo")
    assert n == 7
    cypher = ses.run.call_args[0][0]
    assert "DETACH DELETE i" in cypher


# ---------------------------------------------------------------------------
# list_entity_chunk_id_counts（实体 → chunk_ids 长度）
# ---------------------------------------------------------------------------


def test_list_entity_chunk_id_counts_parses_json(mock_neo4j_store, session_ctx):
    ctx, ses = session_ctx
    # mock data() 返三个实体
    ses.run.return_value.data.return_value = [
        {"name": "AGV", "chunk_ids": _json.dumps(["c1", "c2", "c3", "c4", "c5", "c6"])},  # 6 个，通用词
        {"name": "急停按钮", "chunk_ids": _json.dumps(["c7", "c8"])},  # 2 个，OK
        {"name": "broken_json", "chunk_ids": "not-json"},  # 解析失败 → 0
        {"name": "empty", "chunk_ids": "[]"},
    ]
    mock_neo4j_store._session = lambda: ctx

    out = mock_neo4j_store.list_entity_chunk_id_counts("agv_demo")
    assert out == {"AGV": 6, "急停按钮": 2, "broken_json": 0, "empty": 0}


def test_list_entity_chunk_id_counts_empty_kb(mock_neo4j_store):
    """空 kb_id 直接返空 dict，不调 session。"""
    out = mock_neo4j_store.list_entity_chunk_id_counts("")
    assert out == {}


# ---------------------------------------------------------------------------
# find_images_for_entities：排序 + exclude + limit
# ---------------------------------------------------------------------------


def test_find_images_returns_sorted_by_hits(mock_neo4j_store, session_ctx):
    ctx, ses = session_ctx
    ses.run.return_value.data.return_value = [
        # 已经按 hits DESC 排好
        {"img_id": "img1", "path": "p1", "doc": "DocA",
         "chunk_id": "chunkA", "caption_zh": "z1", "caption_en": "e1", "hits": 3},
        {"img_id": "img2", "path": "p2", "doc": "DocB",
         "chunk_id": "chunkB", "caption_zh": "z2", "caption_en": "e2", "hits": 2},
    ]
    mock_neo4j_store._session = lambda: ctx

    out = mock_neo4j_store.find_images_for_entities(
        "agv_demo", ["E1", "E2"], limit=3,
    )
    assert len(out) == 2
    assert out[0]["img_id"] == "img1"
    assert out[0]["hit_count"] == 3
    assert out[1]["hit_count"] == 2


def test_find_images_respects_exclude_chunk_ids(mock_neo4j_store, session_ctx):
    """命中 chunk 列表里已经有的图不应再返回（避免文本+图重复）。"""
    ctx, ses = session_ctx
    ses.run.return_value.data.return_value = [
        {"img_id": "img1", "path": "p1", "doc": "D",
         "chunk_id": "chunkA", "caption_zh": "z", "caption_en": "e", "hits": 5},
        {"img_id": "img2", "path": "p2", "doc": "D",
         "chunk_id": "chunkB", "caption_zh": "z", "caption_en": "e", "hits": 4},
        {"img_id": "img3", "path": "p3", "doc": "D",
         "chunk_id": "chunkC", "caption_zh": "z", "caption_en": "e", "hits": 3},
    ]
    mock_neo4j_store._session = lambda: ctx

    out = mock_neo4j_store.find_images_for_entities(
        "agv_demo", ["E1"], exclude_chunk_ids=["chunkA", "chunkC"], limit=3,
    )
    # chunkA 和 chunkC 被排除，只剩 img2
    assert [r["img_id"] for r in out] == ["img2"]


def test_find_images_caps_at_limit(mock_neo4j_store, session_ctx):
    ctx, ses = session_ctx
    # mock 返 6 条
    ses.run.return_value.data.return_value = [
        {"img_id": f"img{i}", "path": f"p{i}", "doc": "D",
         "chunk_id": f"c{i}", "caption_zh": "z", "caption_en": "e",
         "hits": 10 - i}
        for i in range(6)
    ]
    mock_neo4j_store._session = lambda: ctx

    out = mock_neo4j_store.find_images_for_entities(
        "agv_demo", ["E1"], limit=3,
    )
    assert len(out) == 3


def test_find_images_empty_entity_names_returns_empty(mock_neo4j_store):
    out = mock_neo4j_store.find_images_for_entities("agv_demo", [], limit=3)
    assert out == []


def test_find_images_empty_kb_id_returns_empty(mock_neo4j_store):
    out = mock_neo4j_store.find_images_for_entities("", ["E1"], limit=3)
    assert out == []
