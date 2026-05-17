"""Phase 7.2.A: PromptRenderer placeholder 替换单测。"""

from __future__ import annotations

import re

import pytest

from custom_app.services.prompt_renderer import render_prompt


class TestBasicReplacement:
    def test_replace_single_placeholder(self):
        out = render_prompt("Hello, {{name}}!", {"name": "World"})
        assert out == "Hello, World!"

    def test_replace_with_spaces_inside_braces(self):
        out = render_prompt("{{ name }} and {{  age }}", {"name": "Bob", "age": 30})
        assert out == "Bob and 30"

    def test_unknown_placeholder_preserved(self):
        out = render_prompt("Hello {{name}}, role={{role}}", {"name": "Bob"})
        assert out == "Hello Bob, role={{role}}"

    def test_empty_template_returns_empty(self):
        assert render_prompt("", None) == ""

    def test_no_context_falls_back_to_autofill(self):
        out = render_prompt("lang={{language}}", None)
        assert "Chinese (Simplified)" in out


class TestAutofill:
    def test_language_defaults_to_simplified_chinese(self):
        out = render_prompt("{{language}}", {})
        assert out == "Chinese (Simplified)"

    def test_current_time_is_iso_utc(self):
        out = render_prompt("now={{current_time}}", {})
        # 2026-05-16T12:34:56Z 风格
        m = re.search(r"now=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", out)
        assert m is not None, out

    def test_context_overrides_autofill(self):
        out = render_prompt("{{language}}", {"language": "English"})
        assert out == "English"

    def test_none_value_in_context_falls_back(self):
        out = render_prompt("{{language}}", {"language": None})
        assert out == "Chinese (Simplified)"


class TestKbPlaceholders:
    def test_kb_name_and_description(self):
        tmpl = (
            "你是 {{kb_name}} 的助手。\n"
            "知识库描述：{{kb_description}}"
        )
        out = render_prompt(
            tmpl,
            {"kb_name": "AGV 文档", "kb_description": "工业 SOP 知识库"},
        )
        assert "AGV 文档" in out
        assert "工业 SOP 知识库" in out

    def test_kb_name_missing_preserves_original_marker(self):
        out = render_prompt("KB: {{kb_name}}", {})
        assert out == "KB: {{kb_name}}"


class TestSafety:
    def test_dollar_signs_not_special(self):
        """re.sub 的 repl 参数若是字符串会处理 \\g<n> / \\1；这里用函数避免风险。"""
        out = render_prompt(
            "price={{value}}", {"value": r"$\1 \g<0> ${X}"}
        )
        assert out == r"price=$\1 \g<0> ${X}"

    def test_repeated_placeholder_replaced_all(self):
        out = render_prompt("{{x}}-{{x}}-{{x}}", {"x": "A"})
        assert out == "A-A-A"

    def test_non_identifier_left_alone(self):
        """{{1abc}} / {{a-b}} 不是合法 identifier，应原样保留。"""
        out = render_prompt("{{1abc}} {{a-b}}", {"1abc": "x", "a-b": "y"})
        assert out == "{{1abc}} {{a-b}}"
