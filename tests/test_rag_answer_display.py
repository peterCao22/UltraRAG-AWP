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


def test_answer_to_blocks_ircot_renders_prose_only_no_postfix_images():
    """IRCoT 渲染：整段散文作 text；不在末尾追加 image blocks。

    根因背景：
      - IRCoT 给 LLM 的 Excerpts 已经把 [IMG: ...] 转成了 ![](URL) markdown
        （见 services/strategies/ircot.py:_build_passages_from_hits）
      - ircot_sop.jinja 规则 #9 要求 LLM 把引用步骤的 ![](URL) 一起复制到答案
      - 所以 LLM 答案 markdown 本身已含图片占位，前端 marked 会按位渲染
      - 后端再后挂 image blocks 会导致"段内图 + 末尾图"两次。

    回归 bug：早期 _answer_to_blocks 把每个 source 都判定为"模型未按分段格式输出
    本节"并退化成 chunk excerpt 原文，用户看到的不是真实推理答案。
    """
    r = RagRunner.__new__(RagRunner)
    raw = (
        "## 库存基础数据 (Basic Data for Inventory)\n\n"
        "该界面共有 5 项配置：① 出库类型 ② 入库类型 ③ 品牌 ④ 零件状态 ⑤ 计划人\n\n"
        "### 计划人 (Planners)\n选择 Planners 页签 → 双击行 → F8 选人 → 保存\n\n"
        "![](/images/IFS/img_005.png)"
    )
    sources = [
        {
            "display_title": "库存基础数据",
            "title": "库存基础数据",
            "source_id": "ifs_inv_section_2",
            "excerpt": "原始 chunk 内容...",
            "images": ["data:image/png;base64,IMG1"],
        },
        {
            "display_title": "库存管理",
            "title": "库存管理",
            "source_id": "ifs_mgmt_section_2",
            "excerpt": "另一份不相关 chunk...",
            "images": ["data:image/png;base64,IMG2"],
        },
    ]
    blocks = RagRunner._answer_to_blocks(r, raw, sources, raw, is_ircot=True)
    text_blocks = [b for b in blocks if b.get("type") == "text"]
    image_blocks = [b for b in blocks if b.get("type") == "image"]
    # 仅 1 个 text，无 image（image markdown 在 text 内由前端 marked 渲染）
    assert len(text_blocks) == 1
    assert len(image_blocks) == 0
    assert "5 项配置" in text_blocks[0]["content"]
    assert "计划人" in text_blocks[0]["content"]
    # markdown 图片占位原样保留（前端 marked 会渲染）
    assert "![](/images/IFS/img_005.png)" in text_blocks[0]["content"]
    # 不应混入 chunk 原文 fallback 文案
    assert "模型未按分段格式输出" not in text_blocks[0]["content"]
    assert "原始 chunk 内容" not in text_blocks[0]["content"]


def test_build_result_from_raw_ircot_path_skips_excerpt_parsing():
    """is_ircot_answer=True 时不应触发 <<<EXCERPT k>>> 分段，answer_plain 直接用原文。"""
    r = RagRunner.__new__(RagRunner)
    r._build_sources = MagicMock(
        return_value=[
            {
                "title": "库存基础数据",
                "display_title": "库存基础数据",
                "source_id": "ifs_inv",
                "snippet": "...",
                "excerpt": "原始 chunk 内容",
                "images": [],
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
        "is_ircot_answer": True,
    }
    raw = "答案是：库存基础数据 5 项配置，其中计划人在 Planners 页签..."
    out = RagRunner._build_result_from_raw(r, prep, raw)
    # answer 字段应包含 LLM 推理答案而不是 chunk excerpt 退化文案
    assert "5 项配置" in out["answer"]
    assert "计划人在 Planners 页签" in out["answer"]
    assert "模型未按分段格式输出" not in out["answer"]
    assert "原始 chunk 内容" not in out["answer"]


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
