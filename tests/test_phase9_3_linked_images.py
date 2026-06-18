"""Phase 9.3.B `_find_linked_images` + prompt 拼接 + meta 透出单测。

覆盖：
  1. ENABLED=0 → skip_reason='disabled'
  2. 空 hit_ids → skip_reason='no_hits'
  3. KG backend 不是 neo4j → skip_reason='kg_backend:sqlite'（9.3.A 只在
     Neo4j 建图，sqlite 后端没有 Image 节点）
  4. KG store 失败 → skip_reason='kg_store_error:...'
  5. 命中 chunk 无实体 → skip_reason='no_entities'
  6. 实体太多 → 截到 max_entities
  7. 有图返回 → 正常路径
  8. find_images 抛异常 → 降级
  9. _build_prompt(linked_images=...) 拼模板
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_app.services.rag_runner import RagRunner


def _make_runner(rows: list[dict[str, Any]]) -> RagRunner:
    r = RagRunner.__new__(RagRunner)
    r.kb_id = "agv_demo"
    r._rows = rows
    return r


# ---------------------------------------------------------------------------
# disabled / 空入参
# ---------------------------------------------------------------------------


def test_disabled_returns_skip(monkeypatch) -> None:
    monkeypatch.setenv("ULTRARAG_PHASE9_3_ENABLED", "0")
    r = _make_runner([{"id": "c1"}])
    imgs, meta = r._find_linked_images([0], kb_id="agv_demo")
    assert imgs == []
    assert meta["skip_reason"] == "disabled"
    assert meta["enabled"] is False


def test_empty_hit_ids(monkeypatch) -> None:
    monkeypatch.delenv("ULTRARAG_PHASE9_3_ENABLED", raising=False)
    r = _make_runner([])
    imgs, meta = r._find_linked_images([], kb_id="agv_demo")
    assert imgs == []
    assert meta["skip_reason"] == "no_hits"


def test_empty_kb_id(monkeypatch) -> None:
    monkeypatch.delenv("ULTRARAG_PHASE9_3_ENABLED", raising=False)
    r = _make_runner([{"id": "c1"}])
    imgs, meta = r._find_linked_images([0], kb_id="")
    assert imgs == []
    assert meta["skip_reason"] == "no_kb_id"


def test_hit_ids_out_of_bounds(monkeypatch) -> None:
    """hit_ids 包含越界索引时 chunk_id 集合为空 → skip。"""
    monkeypatch.delenv("ULTRARAG_PHASE9_3_ENABLED", raising=False)
    monkeypatch.setenv("ULTRARAG_KG_BACKEND", "neo4j")
    r = _make_runner([{"id": "c1"}])
    imgs, meta = r._find_linked_images([99, -1], kb_id="agv_demo")
    assert imgs == []
    assert meta["skip_reason"] == "no_chunk_ids"


# ---------------------------------------------------------------------------
# KG backend 不是 neo4j
# ---------------------------------------------------------------------------


def test_sqlite_backend_skipped(monkeypatch) -> None:
    monkeypatch.delenv("ULTRARAG_PHASE9_3_ENABLED", raising=False)
    monkeypatch.setenv("ULTRARAG_KG_BACKEND", "sqlite")
    r = _make_runner([{"id": "c1"}])
    imgs, meta = r._find_linked_images([0], kb_id="agv_demo")
    assert imgs == []
    assert meta["skip_reason"] == "kg_backend:sqlite"


# ---------------------------------------------------------------------------
# 主路径：mock store
# ---------------------------------------------------------------------------


@pytest.fixture()
def neo4j_env(monkeypatch):
    monkeypatch.delenv("ULTRARAG_PHASE9_3_ENABLED", raising=False)
    monkeypatch.setenv("ULTRARAG_KG_BACKEND", "neo4j")
    yield


def test_no_entities_skip(monkeypatch, neo4j_env) -> None:
    fake_store = MagicMock()
    fake_store.list_entity_names_for_chunks.return_value = []
    monkeypatch.setattr(
        "custom_app.services.kgstore.build_kg_store", lambda *a, **kw: fake_store,
    )
    r = _make_runner([{"id": "c1"}])
    imgs, meta = r._find_linked_images([0], kb_id="agv_demo")
    assert imgs == []
    assert meta["skip_reason"] == "no_entities"
    assert meta["entity_count"] == 0


def test_entities_truncated_to_max(monkeypatch, neo4j_env) -> None:
    """实体多于 max_entities 时截断；find_images 收到截断后的列表。"""
    monkeypatch.setenv("ULTRARAG_PHASE9_3_MAX_ENTITIES", "3")
    fake_store = MagicMock()
    fake_store.list_entity_names_for_chunks.return_value = [
        f"e{i}" for i in range(10)
    ]
    fake_store.find_images_for_entities.return_value = []
    monkeypatch.setattr(
        "custom_app.services.kgstore.build_kg_store", lambda *a, **kw: fake_store,
    )
    r = _make_runner([{"id": "c1"}])
    imgs, meta = r._find_linked_images([0], kb_id="agv_demo")
    fake_store.find_images_for_entities.assert_called_once()
    passed_entities = fake_store.find_images_for_entities.call_args.kwargs.get(
        "entity_names",
    ) or fake_store.find_images_for_entities.call_args.args[1]
    assert len(passed_entities) == 3
    assert meta["entity_count"] == 3


def test_happy_path_returns_images(monkeypatch, neo4j_env) -> None:
    fake_store = MagicMock()
    fake_store.list_entity_names_for_chunks.return_value = ["急停按钮", "Master Link Down"]
    fake_store.find_images_for_entities.return_value = [
        {"img_id": "abc", "path": "p1", "doc": "D1", "chunk_id": "D1_step_5",
         "caption_zh": "急停按钮特写", "caption_en": "Close-up of E-Stop",
         "hit_count": 2},
        {"img_id": "def", "path": "p2", "doc": "D2", "chunk_id": "D2_intro",
         "caption_zh": "Master Link 弹窗", "caption_en": "Master Link popup",
         "hit_count": 1},
    ]
    monkeypatch.setattr(
        "custom_app.services.kgstore.build_kg_store", lambda *a, **kw: fake_store,
    )
    r = _make_runner([
        {"id": "c_alarm"},
        {"id": "c_other"},
    ])
    imgs, meta = r._find_linked_images([0, 1], kb_id="agv_demo")
    assert len(imgs) == 2
    assert imgs[0]["caption_zh"] == "急停按钮特写"
    assert meta["enabled"] is True
    assert meta["entity_count"] == 2
    assert meta["image_count"] == 2
    assert meta["skip_reason"] is None  # 命中时 skip_reason 为 None

    # 验证 find_images_for_entities 传了 exclude_chunk_ids = 命中 chunk
    call_kwargs = fake_store.find_images_for_entities.call_args.kwargs
    assert "c_alarm" in call_kwargs["exclude_chunk_ids"]
    assert "c_other" in call_kwargs["exclude_chunk_ids"]


def test_find_images_raises_returns_empty(monkeypatch, neo4j_env) -> None:
    """find_images_for_entities 抛异常 → 降级返空。"""
    fake_store = MagicMock()
    fake_store.list_entity_names_for_chunks.return_value = ["entity"]
    fake_store.find_images_for_entities.side_effect = RuntimeError("neo4j down")
    monkeypatch.setattr(
        "custom_app.services.kgstore.build_kg_store", lambda *a, **kw: fake_store,
    )
    r = _make_runner([{"id": "c1"}])
    imgs, meta = r._find_linked_images([0], kb_id="agv_demo")
    assert imgs == []
    assert meta["skip_reason"].startswith("find_images_error:")


def test_list_entities_raises_returns_empty(monkeypatch, neo4j_env) -> None:
    fake_store = MagicMock()
    fake_store.list_entity_names_for_chunks.side_effect = RuntimeError("x")
    monkeypatch.setattr(
        "custom_app.services.kgstore.build_kg_store", lambda *a, **kw: fake_store,
    )
    r = _make_runner([{"id": "c1"}])
    imgs, meta = r._find_linked_images([0], kb_id="agv_demo")
    assert imgs == []
    assert meta["skip_reason"].startswith("list_entities_error:")


def test_build_kg_store_fails_returns_empty(monkeypatch, neo4j_env) -> None:
    def boom(*a, **kw):
        raise RuntimeError("kg unreachable")

    monkeypatch.setattr(
        "custom_app.services.kgstore.build_kg_store", boom,
    )
    r = _make_runner([{"id": "c1"}])
    imgs, meta = r._find_linked_images([0], kb_id="agv_demo")
    assert imgs == []
    assert meta["skip_reason"].startswith("kg_store_error:")


def test_no_images_found_skip_reason(monkeypatch, neo4j_env) -> None:
    """实体有但没跨章节图 → skip_reason='no_cross_chapter_images'。"""
    fake_store = MagicMock()
    fake_store.list_entity_names_for_chunks.return_value = ["entity"]
    fake_store.find_images_for_entities.return_value = []
    monkeypatch.setattr(
        "custom_app.services.kgstore.build_kg_store", lambda *a, **kw: fake_store,
    )
    r = _make_runner([{"id": "c1"}])
    imgs, meta = r._find_linked_images([0], kb_id="agv_demo")
    assert imgs == []
    assert meta["skip_reason"] == "no_cross_chapter_images"
    assert meta["entity_count"] == 1


# ---------------------------------------------------------------------------
# _build_prompt(linked_images=...) jinja 拼接
# ---------------------------------------------------------------------------


def test_build_prompt_includes_linked_images_section() -> None:
    from pathlib import Path

    r = RagRunner.__new__(RagRunner)
    r.prompt_dir = Path("prompt")
    r._rows = [{"id": "c1", "title": "T", "contents": "body content"}]

    out = r._build_prompt(
        "test query", [0],
        linked_images=[
            {"path": "p1", "doc": "D1", "chunk_id": "D1_s_1",
             "caption_zh": "急停特写", "caption_en": "E-Stop"},
            {"path": "p2", "doc": "D2", "chunk_id": "D2_s_2",
             "caption_zh": "弹窗", "caption_en": "Popup"},
        ],
    )
    assert "Related image 1" in out
    assert "Related image 2" in out
    assert "急停特写" in out
    assert "do not include them in your section-by-section answer" in out


def test_build_prompt_no_linked_images_section_when_empty() -> None:
    from pathlib import Path

    r = RagRunner.__new__(RagRunner)
    r.prompt_dir = Path("prompt")
    r._rows = [{"id": "c1", "title": "T", "contents": "body"}]

    out = r._build_prompt("test", [0], linked_images=[])
    assert "Related image" not in out
