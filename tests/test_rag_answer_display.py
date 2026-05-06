"""answer_blocks 展示 Markdown 与 sources 精简逻辑的单元测试。"""

import sys
from types import ModuleType
from unittest.mock import MagicMock

# RagRunner 模块顶层依赖 faiss；本文件仅测纯逻辑，注入占位避免环境缺包。
if "faiss" not in sys.modules:
    _faiss_stub = ModuleType("faiss")
    _faiss_stub.read_index = lambda *_a, **_k: None  # type: ignore[misc]
    sys.modules["faiss"] = _faiss_stub

from custom_app.services.rag_runner import (
    RagRunner,
    answer_blocks_to_display_markdown,
    sources_citation_only_for_ui,
)


def test_answer_blocks_to_display_markdown_joins_text_and_images():
    blocks = [
        {"type": "text", "content": "### 第 1 步\n\n按下数字 7"},
        {
            "type": "image",
            "data_url": "data:image/png;base64,AAA",
            "title": "Keypad [1]",
        },
    ]
    md = answer_blocks_to_display_markdown(blocks, "fallback")
    assert "### 第 1 步" in md
    assert "按下数字 7" in md
    assert "data:image/png;base64,AAA" in md
    assert md.startswith("###")


def test_answer_blocks_to_display_markdown_fallback_when_empty_blocks():
    assert answer_blocks_to_display_markdown([], "  仅纯文本  ") == "仅纯文本"


def test_sources_citation_only_strips_images_and_replaces_excerpt():
    src = [
        {
            "source_id": "x1",
            "doc": "a.docx",
            "title": "STEP 1",
            "display_title": "第 1 步",
            "snippet": "LONG ENGLISH",
            "excerpt": "LONG ENGLISH BODY",
            "images": ["data:image/png;base64,ZZZ"],
        }
    ]
    out = sources_citation_only_for_ui(src, note="（见上方）")
    assert len(out) == 1
    assert out[0]["snippet"] == "（见上方）"
    assert out[0]["excerpt"] == "（见上方）"
    assert out[0]["images"] == []
    assert out[0]["source_id"] == "x1"
    assert out[0]["display_title"] == "第 1 步"


def test_answer_blocks_global_no_information_omits_all_images():
    """模板全局拒答时不应再挂检索插图。"""
    r = RagRunner.__new__(RagRunner)
    raw = (
        "<<<EXCERPT 1>>>\n"
        "根据现有文档，未找到与该问题相关的信息，无法回答。\n"
    )
    sources = [
        {
            "display_title": "第 1 步",
            "title": "STEP 1",
            "source_id": "s1",
            "excerpt": "ignored",
            "images": ["data:image/png;base64,AAA"],
        },
    ]
    plain = "根据现有文档，未找到与该问题相关的信息，无法回答。"
    blocks = RagRunner._answer_to_blocks(r, raw, sources, plain)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert all(b.get("type") != "image" for b in blocks)


def test_answer_blocks_per_section_refusal_skips_images_for_that_section_only():
    """一节为「文档中未找到…」时该节不挂图，其它有实质译文的节仍挂图。"""
    r = RagRunner.__new__(RagRunner)
    raw = (
        "<<<EXCERPT 1>>>\n按下急停后检查指示灯。\n\n"
        "<<<EXCERPT 2>>>\n文档中未找到足够相关信息，无法回答该问题。\n"
    )
    sources = [
        {
            "display_title": "第 1 步",
            "title": "STEP 1",
            "source_id": "a",
            "excerpt": "",
            "images": ["data:image/png;base64,ONE"],
        },
        {
            "display_title": "第 2 步",
            "title": "STEP 2",
            "source_id": "b",
            "excerpt": "",
            "images": ["data:image/png;base64,TWO"],
        },
    ]
    blocks = RagRunner._answer_to_blocks(r, raw, sources, "")
    imgs = [b for b in blocks if b.get("type") == "image"]
    assert len(imgs) == 1
    assert imgs[0]["data_url"] == "data:image/png;base64,ONE"


def test_build_result_from_raw_omits_sources_when_no_answer_from_documents():
    """模型声明无法根据文档回答时，sources 应对 UI 为空。"""
    r = RagRunner.__new__(RagRunner)
    r._build_sources = MagicMock(
        return_value=[
            {
                "title": "IFS",
                "display_title": "IFS",
                "source_id": "x",
                "snippet": "snippet",
                "excerpt": "excerpt",
                "images": ["data:image/png;base64,ZZZ"],
            },
        ]
    )
    prep = {
        "hit_ids": [0],
        "rewritten_q": "q",
        "rerank_meta": {},
        "expanded_docs": [],
        "recall_k": 1,
        "final_k": 1,
        "final_k_cfg": 0,
        "requested_agent_mode": "quick",
        "effective_agent_mode": "quick",
        "degraded": False,
        "degrade_reason": None,
    }
    raw = "根据现有文档，未找到与该问题相关的信息，无法回答。"
    out = RagRunner._build_result_from_raw(r, prep, raw)
    assert out["sources"] == []
    assert out["meta"].get("sources_omitted_for_ui") is True
    assert out["meta"].get("no_answer_from_documents") is True
    assert out["meta"].get("retrieval_source_count") == 1
