"""Phase 12.1 reference_resolver 单元测试。

覆盖：
- has_reference_marker 中英文规则检测（正例 / 反例 / 误判边界）
- resolve_references 跳过路径（disabled / 空 / 无历史 / 无 marker）
- LLM 改写采纳路径（mock anthropic）
- confidence 阈值 / fallback / 解析错误的降级
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from custom_app.services.reference_resolver import (
    ResolutionResult,
    _format_history,
    _parse_llm_json,
    _render_prompt,
    has_reference_marker,
    resolve_references,
)


# ─────────────────────────────────────────────────────────────────────────────
# 规则检测
# ─────────────────────────────────────────────────────────────────────────────


class TestHasReferenceMarker:
    @pytest.mark.parametrize(
        "q",
        [
            "它怎么处理？",
            "这个怎么操作",
            "那些步骤呢",
            "他需要重启吗",
            "第 2 个怎么操作",
            "第二个步骤是什么",
            "第十步具体讲什么",
            "继续",
            "然后呢",
            "接着往下走",
            "下一步是什么",
            "上一步的目的",
            "What about it",
            "tell me the second one",
            "What is the 3rd step",
        ],
    )
    def test_positive(self, q: str) -> None:
        assert has_reference_marker(q) is True, f"should detect marker in: {q!r}"

    @pytest.mark.parametrize(
        "q",
        [
            "AGV 怎么启动",
            "请问急停按钮在哪里",
            "我想了解换电流程",
            "How to start AGV",
            "What is the battery replacement procedure",
            "",
            "   ",
            # 易误判的反例：包含"教育"但非"继续教育"上下文也应该谨慎
            # "继续" 是 marker → 不放进反例。这里用真正不含 marker 的句子
            "Battery Block Battery Low 报警怎么处理",
            "如何配置 Planners",
        ],
    )
    def test_negative(self, q: str) -> None:
        assert has_reference_marker(q) is False, f"should NOT detect marker in: {q!r}"

    def test_cn_ordinal_with_chinese_numerals(self) -> None:
        assert has_reference_marker("第三个怎么做")
        assert has_reference_marker("第十一步呢")  # 注：会命中"第十一"
        assert has_reference_marker("第 5 步说什么")

    def test_en_ordinal_word_boundary(self) -> None:
        """英文序数应只匹配独立单词，不应命中 'second' 出现在 'secondary' 里。"""
        # secondary 不应被认为是指代
        assert has_reference_marker("secondary battery") is False
        # the second 应该命中
        assert has_reference_marker("explain the second")
        # the 3rd 应该命中
        assert has_reference_marker("show me the 3rd item")


# ─────────────────────────────────────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────────────────────────────────────


class TestFormatHistory:
    def test_empty(self) -> None:
        assert _format_history([]) == "（无历史）"
        assert _format_history(None or []) == "（无历史）"

    def test_basic_alternating(self) -> None:
        h = [
            {"role": "user", "content": "AGV 启动前要做什么"},
            {"role": "assistant", "content": "1. 检查电池 2. 检查急停 3. 检查导航"},
        ]
        out = _format_history(h)
        assert "用户: AGV 启动前要做什么" in out
        assert "助手: 1. 检查电池" in out

    def test_truncates_long_content(self) -> None:
        long = "甲" * 1000
        h = [{"role": "assistant", "content": long}]
        out = _format_history(h, max_chars_per_turn=100)
        assert "...[truncated]" in out
        assert len(out) < 1000

    def test_skips_empty_content(self) -> None:
        h = [
            {"role": "user", "content": ""},
            {"role": "user", "content": "real query"},
        ]
        out = _format_history(h)
        assert "real query" in out
        # 不应该有空"用户: "
        assert "用户: \n" not in out


class TestRenderPrompt:
    """验证 jinja 模板渲染正确（不依赖 LLM 调用）。"""

    def test_renders_question_and_history(self) -> None:
        history = [
            {"role": "user", "content": "AGV 启动前要做什么？"},
            {"role": "assistant", "content": "1. 检查电池 2. 检查急停"},
        ]
        out = _render_prompt("第 2 个怎么操作？", history)
        # question 嵌入
        assert "第 2 个怎么操作？" in out
        # 历史嵌入
        assert "用户: AGV 启动前要做什么" in out
        assert "助手: 1. 检查电池" in out
        # 规则段保留
        assert "rewritten_query" in out
        assert "confidence" in out
        # 示例段保留
        assert "示例 1" in out

    def test_renders_empty_history(self) -> None:
        out = _render_prompt("它怎么操作", [])
        assert "（无历史）" in out
        assert "它怎么操作" in out


class TestParseLlmJson:
    def test_direct_json(self) -> None:
        out = _parse_llm_json('{"rewritten_query": "abc", "confidence": 0.9}')
        assert out == {"rewritten_query": "abc", "confidence": 0.9}

    def test_with_code_block(self) -> None:
        text = '```json\n{"rewritten_query": "急停按钮", "confidence": 0.95}\n```'
        out = _parse_llm_json(text)
        assert out is not None
        assert out["rewritten_query"] == "急停按钮"

    def test_with_explanation_before_json(self) -> None:
        text = '思考一下 ...\n{"rewritten_query": "x", "confidence": 0.8}'
        out = _parse_llm_json(text)
        assert out is not None
        assert out["confidence"] == 0.8

    def test_invalid_returns_none(self) -> None:
        assert _parse_llm_json("not json at all") is None
        assert _parse_llm_json("") is None
        assert _parse_llm_json(None or "") is None


# ─────────────────────────────────────────────────────────────────────────────
# resolve_references 主入口
# ─────────────────────────────────────────────────────────────────────────────


class TestResolveReferencesSkipPaths:
    """各种跳过 LLM 调用的路径（无 LLM 调用应发生）。"""

    def test_disabled_via_env(self, monkeypatch) -> None:
        monkeypatch.setenv("ULTRARAG_REF_RESOLUTION_ENABLED", "0")
        out = resolve_references("它怎么处理？", [{"role": "user", "content": "前一个问题"}])
        assert out.applied is False
        assert out.skip_reason == "disabled"
        assert out.rewritten_query == "它怎么处理？"

    def test_empty_query(self) -> None:
        out = resolve_references("", [{"role": "user", "content": "x"}])
        assert out.applied is False
        assert out.skip_reason == "empty_query"

    def test_no_history(self) -> None:
        out = resolve_references("它怎么处理？", None)
        assert out.applied is False
        assert out.skip_reason == "no_history"

        out = resolve_references("它怎么处理？", [])
        assert out.applied is False
        assert out.skip_reason == "no_history"

    def test_no_marker_in_query(self) -> None:
        out = resolve_references(
            "AGV 启动前要做什么",
            [{"role": "user", "content": "之前问题"}],
        )
        assert out.applied is False
        assert out.skip_reason == "no_marker"
        # 即便跳过也应保留 original
        assert out.original_query == "AGV 启动前要做什么"
        assert out.rewritten_query == "AGV 启动前要做什么"


class TestResolveReferencesWithMock:
    """mock LLM 调用，验证完整流程。"""

    @pytest.fixture
    def history(self) -> list:
        return [
            {"role": "user", "content": "AGV 启动前要做什么？"},
            {"role": "assistant", "content": "1. 检查电池 2. 检查急停 3. 检查导航"},
        ]

    @pytest.fixture
    def env_setup(self, monkeypatch):
        monkeypatch.setenv("ULTRARAG_REF_RESOLUTION_ENABLED", "1")
        monkeypatch.setenv("ULTRARAG_REF_RESOLUTION_BACKEND", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def test_anthropic_high_confidence_applied(self, history, env_setup) -> None:
        mock_text = (
            '{"rewritten_query": "急停按钮如何检查", "confidence": 0.92, '
            '"resolved": [{"reference": "第 2 个", "meaning": "急停按钮"}]}'
        )
        with patch(
            "custom_app.services.reference_resolver._call_anthropic",
            return_value=(mock_text, "claude-haiku-4-5-20251001"),
        ):
            out = resolve_references("第 2 个怎么操作？", history)
        assert out.applied is True
        assert out.rewritten_query == "急停按钮如何检查"
        assert out.confidence == 0.92
        assert out.model == "claude-haiku-4-5-20251001"
        assert len(out.resolved) == 1
        assert out.resolved[0] == {"reference": "第 2 个", "meaning": "急停按钮"}
        assert out.skip_reason is None

    def test_low_confidence_not_applied(self, history, env_setup) -> None:
        mock_text = '{"rewritten_query": "x", "confidence": 0.4, "resolved": []}'
        with patch(
            "custom_app.services.reference_resolver._call_anthropic",
            return_value=(mock_text, "claude-haiku-4-5-20251001"),
        ):
            out = resolve_references("它呢？", history)
        assert out.applied is False
        assert out.skip_reason == "low_confidence"
        assert out.rewritten_query == "它呢？"  # 保留 original
        assert out.confidence == 0.4

    def test_rewritten_same_as_original_not_applied(self, history, env_setup) -> None:
        mock_text = (
            '{"rewritten_query": "它呢？", "confidence": 0.9, "resolved": []}'
        )
        with patch(
            "custom_app.services.reference_resolver._call_anthropic",
            return_value=(mock_text, "claude-haiku-4-5-20251001"),
        ):
            out = resolve_references("它呢？", history)
        assert out.applied is False
        assert out.skip_reason == "no_change"

    def test_anthropic_failure_falls_back_to_gemini(self, history, env_setup, monkeypatch) -> None:
        monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
        mock_text = '{"rewritten_query": "急停按钮如何检查", "confidence": 0.85, "resolved": []}'
        with patch(
            "custom_app.services.reference_resolver._call_anthropic",
            side_effect=RuntimeError("anthropic down"),
        ), patch(
            "custom_app.services.reference_resolver._call_gemini",
            return_value=(mock_text, "gemini-2.0-flash"),
        ):
            out = resolve_references("第 2 个怎么操作？", history)
        assert out.applied is True
        assert out.model == "gemini-2.0-flash"

    def test_both_backends_fail_returns_skip(self, history, env_setup, monkeypatch) -> None:
        monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
        with patch(
            "custom_app.services.reference_resolver._call_anthropic",
            side_effect=RuntimeError("anthropic down"),
        ), patch(
            "custom_app.services.reference_resolver._call_gemini",
            side_effect=RuntimeError("gemini down"),
        ):
            out = resolve_references("第 2 个怎么操作？", history)
        assert out.applied is False
        assert out.skip_reason and out.skip_reason.startswith("error:")
        assert out.rewritten_query == "第 2 个怎么操作？"

    def test_no_anthropic_key_skips_to_gemini(self, history, monkeypatch) -> None:
        monkeypatch.setenv("ULTRARAG_REF_RESOLUTION_ENABLED", "1")
        monkeypatch.setenv("ULTRARAG_REF_RESOLUTION_BACKEND", "anthropic")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ULTRARAG_ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
        mock_text = '{"rewritten_query": "x reference", "confidence": 0.9, "resolved": []}'
        with patch(
            "custom_app.services.reference_resolver._call_gemini",
            return_value=(mock_text, "gemini-2.0-flash"),
        ):
            out = resolve_references("它呢？", history)
        assert out.applied is True
        assert out.model == "gemini-2.0-flash"

    def test_parse_error_skip(self, history, env_setup) -> None:
        with patch(
            "custom_app.services.reference_resolver._call_anthropic",
            return_value=("not a valid json {bad}", "claude-haiku-4-5-20251001"),
        ):
            out = resolve_references("它呢？", history)
        assert out.applied is False
        assert out.skip_reason == "parse_error"

    def test_resolved_filters_invalid_items(self, history, env_setup) -> None:
        """resolved 数组里非 dict 或缺字段的项应被过滤。"""
        mock_text = (
            '{"rewritten_query": "急停按钮如何检查", "confidence": 0.9, '
            '"resolved": ['
            '  {"reference": "第 2 个", "meaning": "急停按钮"},'
            '  {"reference": "缺 meaning"},'
            '  "non-dict-item",'
            '  {"meaning": "缺 reference"}'
            ']}'
        )
        with patch(
            "custom_app.services.reference_resolver._call_anthropic",
            return_value=(mock_text, "claude-haiku-4-5-20251001"),
        ):
            out = resolve_references("第 2 个怎么操作？", history)
        assert out.applied is True
        assert len(out.resolved) == 1
        assert out.resolved[0]["reference"] == "第 2 个"


class TestResolutionResultToMeta:
    def test_meta_shape(self) -> None:
        r = ResolutionResult(
            applied=True,
            original_query="它呢",
            rewritten_query="急停按钮如何检查",
            confidence=0.925,
            resolved=[{"reference": "它", "meaning": "急停按钮"}],
            ms=512,
            model="claude-haiku-4-5-20251001",
        )
        m = r.to_meta()
        assert m["applied"] is True
        assert m["confidence"] == 0.925  # 四舍五入到 3 位
        assert m["ms"] == 512
        assert "raw_llm_text" not in m  # debug 字段不暴露
