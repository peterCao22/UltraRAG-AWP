"""Phase 11.1.5 Query 意图分类单元测试。

覆盖：
  1. 规则层：chitchat / help / data_query 各类命中
  2. 规则未命中 → LLM 兜底（mock）
  3. LLM 失败 → 降级 knowledge
  4. LLM 低置信度 → 降级 knowledge
  5. ENABLED=0 → 直接 knowledge (source='disabled')
  6. LLM_FALLBACK=0 → 规则未命中也走 knowledge (source='fallback')
  7. 含问候 + 业务问题 → 规则不命中（长度 > 12 字）让 LLM 兜底
  8. canned_responses：4 种意图的返回
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from custom_app.services.intent import (
    INTENT_CHITCHAT,
    INTENT_DATA_QUERY,
    INTENT_HELP,
    INTENT_KNOWLEDGE,
    classify_intent,
    get_canned_response,
)


# ---------------------------------------------------------------------------
# 规则层：chitchat
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("q", [
    "你好",
    "您好",
    "谢谢",
    "感谢",
    "hello",
    "hi",
    "Thanks!",
    "再见",
])
def test_rule_classifies_chitchat(q):
    r = classify_intent(q)
    assert r.intent == INTENT_CHITCHAT
    assert r.source == "rule"
    assert r.confidence >= 0.8


def test_rule_chitchat_only_short_strings():
    """问候词嵌入到长 query（>12 字）时不被误判为 chitchat。"""
    # 这种长 query 走 LLM；测试里我们 monkeypatch LLM 返回 knowledge
    r = classify_intent("你好，AGV 电池组下降按钮怎么操作")
    # 没 mock LLM，会走 fallback → knowledge
    assert r.intent == INTENT_KNOWLEDGE


# ---------------------------------------------------------------------------
# 规则层：help
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("q", [
    "你能做什么？",
    "你能帮我做什么",
    "你会什么？",
    "你是谁",
    "帮助",
    "help",
    "What can you do?",
    "how do i use this",
])
def test_rule_classifies_help(q):
    r = classify_intent(q)
    assert r.intent == INTENT_HELP
    assert r.source == "rule"


# ---------------------------------------------------------------------------
# 规则层：data_query
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("q", [
    "本月 AGV 故障次数",
    "最近 7 天告警统计",
    "今天有多少故障",
    "总计有多少台 AGV",
    "How many failures last 30 days",
    "total count of alarms",
])
def test_rule_classifies_data_query(q):
    r = classify_intent(q)
    assert r.intent == INTENT_DATA_QUERY
    assert r.source == "rule"


# ---------------------------------------------------------------------------
# 规则不命中 → LLM 兜底
# ---------------------------------------------------------------------------


def test_rule_miss_falls_back_to_llm_knowledge(monkeypatch):
    """常规知识问题规则不命中，LLM 返回 knowledge 高置信度。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(
        "custom_app.services.intent.classifier._call_anthropic",
        MagicMock(return_value=(
            json.dumps({"intent": "knowledge", "confidence": 0.85, "reason": "x"}),
            "claude-haiku",
        )),
    )
    # 防真实 Gemini fallback
    monkeypatch.setattr(
        "custom_app.services.intent.classifier._call_gemini",
        MagicMock(return_value=("", "")),
    )
    r = classify_intent("AGV 启动后第九步要确认什么")
    assert r.intent == INTENT_KNOWLEDGE
    assert r.source == "llm"
    assert r.confidence >= 0.5


def test_llm_low_confidence_falls_back_to_knowledge(monkeypatch):
    """LLM 给低置信度 → 降级 knowledge，source='fallback'。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(
        "custom_app.services.intent.classifier._call_anthropic",
        MagicMock(return_value=(
            json.dumps({"intent": "chitchat", "confidence": 0.3, "reason": "x"}),
            "claude-haiku",
        )),
    )
    monkeypatch.setattr(
        "custom_app.services.intent.classifier._call_gemini",
        MagicMock(return_value=("", "")),
    )
    r = classify_intent("某个无法用规则归类的较长 query 测试")
    assert r.intent == INTENT_KNOWLEDGE
    assert r.source == "fallback"


def test_llm_invalid_intent_falls_back(monkeypatch):
    """LLM 返回非法 intent 名 → 降级。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(
        "custom_app.services.intent.classifier._call_anthropic",
        MagicMock(return_value=(
            json.dumps({"intent": "some_other_label", "confidence": 0.9}),
            "claude-haiku",
        )),
    )
    monkeypatch.setattr(
        "custom_app.services.intent.classifier._call_gemini",
        MagicMock(return_value=("", "")),
    )
    r = classify_intent("某种较长 query 测试 LLM 误输出")
    assert r.intent == INTENT_KNOWLEDGE
    assert r.source == "fallback"


def test_llm_call_failure_falls_back(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(
        "custom_app.services.intent.classifier._call_anthropic",
        MagicMock(side_effect=RuntimeError("anthropic down")),
    )
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("ULTRARAG_GEMINI_API_KEY", "")
    fake_repo = MagicMock()
    fake_repo.list_active.return_value = []
    monkeypatch.setattr(
        "custom_app.repositories.chat_model_repository.ChatModelRepository",
        lambda *a, **kw: fake_repo,
    )

    r = classify_intent("某种较长 query 触发 LLM 兜底")
    assert r.intent == INTENT_KNOWLEDGE
    assert r.source == "fallback"


# ---------------------------------------------------------------------------
# env 开关
# ---------------------------------------------------------------------------


def test_env_disabled_returns_knowledge_with_source_disabled(monkeypatch):
    monkeypatch.setenv("ULTRARAG_INTENT_ENABLED", "0")
    r = classify_intent("你好")
    assert r.intent == INTENT_KNOWLEDGE
    assert r.source == "disabled"


def test_env_llm_fallback_disabled_skips_llm(monkeypatch):
    """ULTRARAG_INTENT_LLM_FALLBACK=0 时规则未命中直接 knowledge，零 token。"""
    monkeypatch.setenv("ULTRARAG_INTENT_LLM_FALLBACK", "0")
    # Mock LLM 以确保如果被调用会报错（应该不被调用）
    mock_llm = MagicMock(side_effect=RuntimeError("should not be called"))
    monkeypatch.setattr(
        "custom_app.services.intent.classifier._call_anthropic", mock_llm,
    )

    r = classify_intent("某种较长 query 触发兜底")
    assert r.intent == INTENT_KNOWLEDGE
    assert r.source == "fallback"
    mock_llm.assert_not_called()


def test_empty_query_returns_knowledge():
    r = classify_intent("")
    assert r.intent == INTENT_KNOWLEDGE


# ---------------------------------------------------------------------------
# canned_responses
# ---------------------------------------------------------------------------


def test_canned_response_chitchat():
    out = get_canned_response(INTENT_CHITCHAT)
    assert out is not None
    assert "AGV" in out or "SOP" in out


def test_canned_response_help_lists_capabilities():
    out = get_canned_response(INTENT_HELP)
    assert out is not None
    assert "SOP" in out
    assert "告警" in out or "Alarm" in out


def test_canned_response_data_query_mentions_in_development():
    out = get_canned_response(INTENT_DATA_QUERY)
    assert out is not None
    assert "开发中" in out


def test_canned_response_knowledge_returns_none():
    """knowledge 意图不返回模板（走 RAG）。"""
    out = get_canned_response(INTENT_KNOWLEDGE)
    assert out is None


def test_canned_response_unknown_intent_returns_none():
    out = get_canned_response("some_other_label")
    assert out is None


# ---------------------------------------------------------------------------
# to_meta 契约
# ---------------------------------------------------------------------------


def test_to_meta_shape():
    r = classify_intent("你好")
    m = r.to_meta()
    assert set(m.keys()) == {"intent", "confidence", "source", "ms"}
    assert m["intent"] in ("chitchat", "help", "data_query", "knowledge")
    assert 0.0 <= m["confidence"] <= 1.0
    assert isinstance(m["ms"], int)
