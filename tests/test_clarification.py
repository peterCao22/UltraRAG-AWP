"""Phase 12.3 Clarification 单元测试。

覆盖：
  1. 触发器 1（低分）：top_score < 阈值 → 触发
  2. 触发器 2（跨域）：top-N 命中 ≥2 个 doc → 触发
  3. 两者并存：trigger_reasons 含两项
  4. 都不满足 → 不触发
  5. 边界：空 hit_ids / rerank 未启用（无分数）/ 仅命中 1 个 doc
  6. 选项文案：单 doc / 跨 2 doc / 跨 3+ doc 的反问句子
  7. doc label 简化：去 ' SOP' 后缀、下划线变空格、过长截断
  8. env 关闭开关
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_app.services.clarification import (
    ClarificationProposal,
    _format_doc_label,
    propose_clarification,
)


def _rows(*doc_names: str) -> list[dict[str, Any]]:
    """构造按顺序的 _rows[i] = {doc: name}，方便 hit_ids 用索引引用。"""
    return [{"id": f"chunk_{i}", "doc": d, "contents": "x", "title": d}
            for i, d in enumerate(doc_names)]


# ---------------------------------------------------------------------------
# 触发器 1：低分
# ---------------------------------------------------------------------------


def test_low_score_triggers_clarification():
    rows = _rows("DocA", "DocA")
    meta = {"rerank_applied": True, "rerank_top_score": 0.15}
    out = propose_clarification(
        question="x",
        hit_ids=[0, 1],
        rows=rows,
        rerank_meta=meta,
    )
    assert out.triggered is True
    assert any(r.startswith("low_score") for r in out.trigger_reasons)


def test_high_score_single_doc_no_trigger():
    rows = _rows("DocA", "DocA")
    meta = {"rerank_applied": True, "rerank_top_score": 0.85}
    out = propose_clarification(
        question="x", hit_ids=[0, 1], rows=rows, rerank_meta=meta,
    )
    assert out.triggered is False


def test_low_score_only_skipped_when_rerank_disabled():
    """rerank_applied=False 时不按分数触发（无可靠分数）。"""
    rows = _rows("DocA")
    meta = {"rerank_applied": False, "rerank_top_score": 0.0}
    out = propose_clarification(
        question="x", hit_ids=[0], rows=rows, rerank_meta=meta,
    )
    # 单 doc + 没 rerank → 既不低分触发也不跨域触发
    assert out.triggered is False


# ---------------------------------------------------------------------------
# 触发器 2：跨域
# ---------------------------------------------------------------------------


def test_cross_domain_two_docs_triggers():
    rows = _rows("AGV SOP", "IFS SOP")
    meta = {"rerank_applied": True, "rerank_top_score": 0.85}  # 分数高
    out = propose_clarification(
        question="怎么充电？", hit_ids=[0, 1], rows=rows, rerank_meta=meta,
    )
    assert out.triggered is True
    assert any(r.startswith("cross_domain") for r in out.trigger_reasons)
    assert len(out.options) == 2
    # 文案应包含两个标签且用 "还是" 连接
    assert "还是" in out.question_text


def test_cross_domain_three_docs_uses_list_format():
    rows = _rows("DocA", "DocB", "DocC")
    meta = {"rerank_applied": True, "rerank_top_score": 0.85}
    out = propose_clarification(
        question="充电", hit_ids=[0, 1, 2], rows=rows, rerank_meta=meta,
    )
    assert out.triggered is True
    # 三 doc 时用列表格式
    assert "请选择一个" in out.question_text
    assert len(out.options) == 3


def test_cross_domain_caps_at_max_options(monkeypatch):
    """4+ doc 时选项数被 MAX_OPTIONS 限制（默认 3）。"""
    monkeypatch.delenv("ULTRARAG_CLARIFICATION_MAX_OPTIONS", raising=False)
    rows = _rows("D1", "D2", "D3", "D4", "D5")
    meta = {"rerank_applied": True, "rerank_top_score": 0.85}
    out = propose_clarification(
        question="x", hit_ids=[0, 1, 2, 3, 4], rows=rows, rerank_meta=meta,
    )
    assert out.triggered is True
    assert len(out.options) == 3


def test_same_doc_repeated_hits_no_cross_domain():
    """同一 doc 多次命中不算跨域。"""
    rows = _rows("DocA", "DocA", "DocA")
    meta = {"rerank_applied": True, "rerank_top_score": 0.85}
    out = propose_clarification(
        question="x", hit_ids=[0, 1, 2], rows=rows, rerank_meta=meta,
    )
    assert out.triggered is False


# ---------------------------------------------------------------------------
# 两者并存
# ---------------------------------------------------------------------------


def test_low_score_and_cross_domain_both_in_reasons():
    rows = _rows("DocA", "DocB")
    meta = {"rerank_applied": True, "rerank_top_score": 0.10}
    out = propose_clarification(
        question="x", hit_ids=[0, 1], rows=rows, rerank_meta=meta,
    )
    assert out.triggered is True
    reasons = " ".join(out.trigger_reasons)
    assert "low_score" in reasons
    assert "cross_domain" in reasons


# ---------------------------------------------------------------------------
# 边界 / 不触发
# ---------------------------------------------------------------------------


def test_empty_hit_ids_no_trigger():
    out = propose_clarification(
        question="x", hit_ids=[], rows=_rows(), rerank_meta={},
    )
    assert out.triggered is False


def test_low_score_single_doc_template_fallback():
    """低分但单 doc 命中：选项 fallback 到 [doc, '其他']。"""
    rows = _rows("DocA")
    meta = {"rerank_applied": True, "rerank_top_score": 0.10}
    out = propose_clarification(
        question="x", hit_ids=[0], rows=rows, rerank_meta=meta,
    )
    assert out.triggered is True
    assert "DocA" in out.options[0] or "其他" in out.options
    assert "匹配较弱" in out.question_text or "更具体" in out.question_text


def test_env_disabled(monkeypatch):
    monkeypatch.setenv("ULTRARAG_CLARIFICATION_ENABLED", "0")
    rows = _rows("DocA", "DocB")
    meta = {"rerank_applied": True, "rerank_top_score": 0.05}
    out = propose_clarification(
        question="x", hit_ids=[0, 1], rows=rows, rerank_meta=meta,
    )
    assert out.triggered is False
    assert "disabled" in out.trigger_reasons


def test_custom_threshold_via_env(monkeypatch):
    """阈值可调：0.5 时 0.4 也触发。"""
    monkeypatch.setenv("ULTRARAG_CLARIFICATION_SCORE_THRESHOLD", "0.5")
    rows = _rows("DocA", "DocA")
    meta = {"rerank_applied": True, "rerank_top_score": 0.4}
    out = propose_clarification(
        question="x", hit_ids=[0, 1], rows=rows, rerank_meta=meta,
    )
    assert out.triggered is True


# ---------------------------------------------------------------------------
# Label 简化
# ---------------------------------------------------------------------------


def test_format_doc_label_strips_sop_suffix():
    assert _format_doc_label("Alarm Block Battery Low SOP") == "Alarm Block Battery Low"
    assert _format_doc_label("BatteryChange_SOP") == "BatteryChange"


def test_format_doc_label_replaces_underscores():
    assert _format_doc_label("Right_Arm_FTC_SOP") == "Right Arm FTC"


def test_format_doc_label_truncates_too_long():
    long = "A" * 100
    out = _format_doc_label(long)
    assert len(out) <= 50


# ---------------------------------------------------------------------------
# 输出契约：to_meta 字段稳定（前端 SSE 解析依赖）
# ---------------------------------------------------------------------------


def test_to_meta_shape_when_triggered():
    rows = _rows("DocA", "DocB")
    meta = {"rerank_applied": True, "rerank_top_score": 0.10}
    out = propose_clarification(
        question="x", hit_ids=[0, 1], rows=rows, rerank_meta=meta,
    )
    m = out.to_meta()
    assert set(m.keys()) == {
        "triggered", "question_text", "options",
        "trigger_reasons", "top_score", "cross_docs",
    }
    assert m["triggered"] is True
    assert isinstance(m["options"], list)
    assert isinstance(m["cross_docs"], list)


def test_to_meta_shape_when_not_triggered():
    out = ClarificationProposal(triggered=False)
    m = out.to_meta()
    # 即便不触发，字段也齐全（前端解析不需要做 None 防御）
    assert m["triggered"] is False
    assert m["question_text"] == ""
    assert m["options"] == []
