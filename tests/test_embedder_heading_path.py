"""Phase 4.3 — heading_path 嵌入增强单元测试。

验证 compose_doc_embedding_text() 拼接逻辑：
    - 含 heading_path 的 chunk：前缀 "A > B\n<title>\n<contents>"
    - 无 heading_path：退化到 Phase 3 行为 "<title>\n<contents>"
    - 老 chunks.jsonl 无 structure 字段：兼容退化
"""

from __future__ import annotations

import pytest

from custom_app.services.google_embedder import (
    IMAGES_MARK,
    compose_doc_embedding_text,
    strip_images_footer,
)


# ---------------------------------------------------------------------------
# heading_path 拼接规则
# ---------------------------------------------------------------------------


def test_legacy_chunk_no_structure_field():
    """Phase 3 chunks.jsonl 无 structure 字段：仅 title + contents。"""
    row = {
        "id": "doc_step_1",
        "title": "STEP 1: 准备工具",
        "contents": "请确认电池电压正常。",
    }
    text = compose_doc_embedding_text(row)
    assert text == "STEP 1: 准备工具\n请确认电池电压正常。"


def test_empty_heading_path_falls_back_to_phase3():
    """structure.heading_path 是空列表：行为等价于无该字段。"""
    row = {
        "title": "T",
        "contents": "C",
        "structure": {"heading_path": []},
    }
    assert compose_doc_embedding_text(row) == "T\nC"


def test_single_level_heading_path():
    row = {
        "title": "电池告警",
        "contents": "告警代码 0x1A。",
        "structure": {"heading_path": ["故障处理"]},
    }
    text = compose_doc_embedding_text(row)
    assert text == "故障处理\n电池告警\n告警代码 0x1A。"


def test_multi_level_heading_path_joined_with_arrow():
    row = {
        "title": "3.1 电池告警",
        "contents": "告警处理流程。",
        "structure": {"heading_path": ["第3章 故障处理", "3.1 电池告警"]},
    }
    text = compose_doc_embedding_text(row)
    assert text == "第3章 故障处理 > 3.1 电池告警\n3.1 电池告警\n告警处理流程。"


def test_heading_path_filters_empty_strings():
    """heading_path 中的空字符串应被过滤。"""
    row = {
        "title": "T",
        "contents": "C",
        "structure": {"heading_path": ["A", "", "  ", "B"]},
    }
    text = compose_doc_embedding_text(row)
    assert text == "A > B\nT\nC"


def test_heading_path_accepts_tuple():
    """heading_path 是 tuple（Chunk.from_jsonl_dict 输出）也能处理。"""
    row = {
        "title": "T",
        "contents": "C",
        "structure": {"heading_path": ("A", "B")},
    }
    text = compose_doc_embedding_text(row)
    assert text == "A > B\nT\nC"


# ---------------------------------------------------------------------------
# 与现有 IMAGES_MARK / strip_images_footer 的兼容
# ---------------------------------------------------------------------------


def test_strips_images_footer_before_compose():
    """contents 末尾的 [IMAGES] footer 应被剥离，不进入嵌入文本。"""
    row = {
        "title": "T",
        "contents": f"正文内容{IMAGES_MARK}images/x.png",
        "structure": {"heading_path": ["A"]},
    }
    text = compose_doc_embedding_text(row)
    assert "images/x.png" not in text
    assert "[IMAGES]" not in text
    assert text == "A\nT\n正文内容"


# ---------------------------------------------------------------------------
# 边界场景
# ---------------------------------------------------------------------------


def test_only_heading_path_no_title_no_body():
    row = {
        "title": "",
        "contents": "",
        "structure": {"heading_path": ["A", "B"]},
    }
    assert compose_doc_embedding_text(row) == "A > B"


def test_only_title():
    row = {"title": "T", "contents": "", "structure": {}}
    assert compose_doc_embedding_text(row) == "T"


def test_only_contents():
    row = {"title": "", "contents": "body"}
    assert compose_doc_embedding_text(row) == "body"


def test_completely_empty_chunk():
    row = {}
    assert compose_doc_embedding_text(row) == ""


def test_chunk_with_none_structure():
    """structure 为 None 时应不抛错。"""
    row = {"title": "T", "contents": "C", "structure": None}
    assert compose_doc_embedding_text(row) == "T\nC"


# ---------------------------------------------------------------------------
# 零回归：Phase 3 chunks.jsonl 嵌入输出应保持一致
# ---------------------------------------------------------------------------


def test_phase3_format_matches_legacy_concat():
    """Phase 3 老 chunks（无 structure）应与旧拼接 `title + "\\n" + body` 等价。"""
    row = {"title": "标题", "contents": "正文"}
    legacy = (row["title"] + "\n" + strip_images_footer(row["contents"])).strip()
    new = compose_doc_embedding_text(row)
    assert new == legacy


def test_build_embedding_npy_uses_compose_func(monkeypatch, tmp_path):
    """build_embedding_npy 调用 compose_doc_embedding_text 而非内联旧拼接。"""
    import json as _json
    import custom_app.services.google_embedder as ge

    chunks = [
        {"id": "a", "title": "T", "contents": "C", "structure": {"heading_path": ["H"]}},
    ]
    chunks_path = tmp_path / "chunks.jsonl"
    chunks_path.write_text(
        "\n".join(_json.dumps(c, ensure_ascii=False) for c in chunks),
        encoding="utf-8",
    )

    captured: list[list[str]] = []

    def fake_embed_texts(texts, task_type="RETRIEVAL_DOCUMENT"):
        import numpy as np

        captured.append(list(texts))
        return np.zeros((len(texts), 768), dtype="float32")

    monkeypatch.setattr(ge, "embed_texts", fake_embed_texts)

    out_path = tmp_path / "emb.npy"
    ge.build_embedding_npy(str(chunks_path), str(out_path))

    assert captured, "embed_texts should have been called"
    # 验证传给 embed_texts 的文本包含 heading_path 前缀
    assert captured[0][0] == "H\nT\nC"
