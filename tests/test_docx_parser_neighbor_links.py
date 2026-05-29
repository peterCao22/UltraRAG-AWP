"""Phase 11.3 docx_parser.link_neighbors_in_place 单元测试。

确认邻居链注入：
  - doc 内部按出现顺序两两连接
  - 首/尾 chunk prev/next 为空字符串
  - 跨 doc 不连接（严格 doc 边界）
  - 缺 doc 字段的 chunk 自成独立组
  - 幂等：多次调用结果一致
"""

from __future__ import annotations

from typing import Any, Dict, List

from custom_app.services.docx_parser import link_neighbors_in_place


def _make_chunk(cid: str, doc: str) -> Dict[str, Any]:
    return {"id": cid, "doc": doc, "title": cid, "contents": "x"}


def test_single_doc_chunks_form_linear_chain():
    chunks = [_make_chunk(f"A_{i}", "A") for i in range(1, 4)]
    link_neighbors_in_place(chunks)
    assert chunks[0]["prev_chunk_id"] == ""
    assert chunks[0]["next_chunk_id"] == "A_2"
    assert chunks[1]["prev_chunk_id"] == "A_1"
    assert chunks[1]["next_chunk_id"] == "A_3"
    assert chunks[2]["prev_chunk_id"] == "A_2"
    assert chunks[2]["next_chunk_id"] == ""


def test_multi_doc_chunks_dont_cross_boundary():
    """跨 doc 不连接：A 的尾 chunk next 为空，B 的首 chunk prev 为空。"""
    chunks: List[Dict[str, Any]] = [
        _make_chunk("A_1", "A"),
        _make_chunk("A_2", "A"),
        _make_chunk("B_1", "B"),
        _make_chunk("B_2", "B"),
    ]
    link_neighbors_in_place(chunks)
    a_last = chunks[1]
    b_first = chunks[2]
    assert a_last["next_chunk_id"] == ""  # A 链尾
    assert b_first["prev_chunk_id"] == ""  # B 链头


def test_single_chunk_doc_has_empty_neighbors():
    chunks = [_make_chunk("solo", "X")]
    link_neighbors_in_place(chunks)
    assert chunks[0]["prev_chunk_id"] == ""
    assert chunks[0]["next_chunk_id"] == ""


def test_empty_list_is_noop():
    chunks: List[Dict[str, Any]] = []
    link_neighbors_in_place(chunks)
    assert chunks == []


def test_missing_doc_field_groups_separately():
    """缺 doc 字段的 chunk 被归入空字符串组，与其他 doc 分离。"""
    chunks: List[Dict[str, Any]] = [
        {"id": "A_1", "doc": "A", "title": "A1", "contents": "x"},
        {"id": "orphan_1", "title": "o1", "contents": "x"},
        {"id": "orphan_2", "title": "o2", "contents": "x"},
    ]
    link_neighbors_in_place(chunks)
    # A 单 chunk → 空邻居
    assert chunks[0]["prev_chunk_id"] == ""
    assert chunks[0]["next_chunk_id"] == ""
    # 两个 orphan 在同组（空字符串 doc），互相连接
    assert chunks[1]["next_chunk_id"] == "orphan_2"
    assert chunks[2]["prev_chunk_id"] == "orphan_1"


def test_link_is_idempotent():
    """多次调用结果一致（增量 parse 场景：parse_docx 内已调，parse_directory 再调）。"""
    chunks = [_make_chunk(f"A_{i}", "A") for i in range(1, 4)]
    link_neighbors_in_place(chunks)
    snapshot = [(c["prev_chunk_id"], c["next_chunk_id"]) for c in chunks]
    link_neighbors_in_place(chunks)
    again = [(c["prev_chunk_id"], c["next_chunk_id"]) for c in chunks]
    assert snapshot == again


def test_interleaved_doc_order_preserves_appearance_order():
    """A_1, B_1, A_2, B_2 → A 内部 A_1 ↔ A_2，B 内部 B_1 ↔ B_2。"""
    chunks: List[Dict[str, Any]] = [
        _make_chunk("A_1", "A"),
        _make_chunk("B_1", "B"),
        _make_chunk("A_2", "A"),
        _make_chunk("B_2", "B"),
    ]
    link_neighbors_in_place(chunks)
    a1, b1, a2, b2 = chunks
    assert a1["next_chunk_id"] == "A_2"
    assert a2["prev_chunk_id"] == "A_1"
    assert b1["next_chunk_id"] == "B_2"
    assert b2["prev_chunk_id"] == "B_1"
