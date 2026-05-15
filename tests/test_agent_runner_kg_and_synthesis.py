"""
回归测试：AgentRunner 在 KG 空表 / 综合失败时的行为，以及 logging_setup 幂等性。

覆盖：
  - KG 表为空时，_effective_enabled_tools 不再包含 query_knowledge_graph
  - KG 表非空时，工具集合保持原样
  - _synthesize_final_answer 在多种失败路径下返回结构化 dict
  - 主循环耗尽轮次且综合失败时，最终 chunk 含可读理由 + 证据摘要
  - setup_logging 重复调用时 root handler 数量稳定（不重复挂）
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest


# ────────────────────────────────────────────────────────────────────
# 公共工厂
# ────────────────────────────────────────────────────────────────────

def _make_runner(*, enabled_tools=None, kg_available: bool = False):
    """构造一个跳过 init() 的 AgentRunner，注入最小依赖。"""
    from custom_app.services.agent_runner import AgentRunner
    from custom_app.services.tools.registry import ToolRegistry
    from custom_app.services.tools.final_answer import FinalAnswerTool

    r = AgentRunner.__new__(AgentRunner)
    r.kb_id = "test_kb"
    r.max_iterations = 2
    r.enabled_tools = enabled_tools
    r._rows = []
    r._index = MagicMock()
    r._kb_name = "测试库"
    r._adapter = MagicMock()
    r._gemini_tools = None
    r._kg_available = kg_available

    # 注册一个最小 registry（含全部工具名，便于 _effective_enabled_tools 展开）
    reg = ToolRegistry()

    class _Stub:
        openai_schema = {"type": "function", "function": {"name": ""}}

        def __init__(self, name):
            self.name = name

    for n in ("knowledge_search", "keyword_search",
              "list_knowledge_chunks", "query_knowledge_graph"):
        reg.register(_Stub(n))
    reg.register(FinalAnswerTool())
    r._registry = reg
    return r


# ────────────────────────────────────────────────────────────────────
# KG 空表 → 工具集合过滤
# ────────────────────────────────────────────────────────────────────

class TestEffectiveEnabledToolsKgFilter:
    def test_kg_empty_removes_query_knowledge_graph_from_explicit_list(self):
        r = _make_runner(
            enabled_tools=[
                "knowledge_search", "list_knowledge_chunks",
                "query_knowledge_graph", "final_answer",
            ],
            kg_available=False,
        )
        result = r._effective_enabled_tools()
        assert "query_knowledge_graph" not in result
        assert "knowledge_search" in result
        assert "final_answer" in result

    def test_kg_empty_with_none_enabled_tools_expands_and_filters(self):
        # enabled_tools=None 表示"全部"。空 KG 时也必须把 KG 工具剔除。
        r = _make_runner(enabled_tools=None, kg_available=False)
        result = r._effective_enabled_tools()
        assert result is not None
        assert "query_knowledge_graph" not in result
        assert "knowledge_search" in result

    def test_kg_available_keeps_query_knowledge_graph(self):
        r = _make_runner(
            enabled_tools=[
                "knowledge_search", "query_knowledge_graph", "final_answer",
            ],
            kg_available=True,
        )
        result = r._effective_enabled_tools()
        assert "query_knowledge_graph" in result

    def test_kg_available_with_none_returns_none_unchanged(self):
        # 全部启用且 KG 可用 → 返回 None（让 registry 走默认全量）
        r = _make_runner(enabled_tools=None, kg_available=True)
        assert r._effective_enabled_tools() is None


class TestDetectKgAvailable:
    def test_returns_false_when_stats_zero(self):
        r = _make_runner()
        with patch(
            "custom_app.services.kg_search.get_graph_stats",
            return_value={"entity_count": 0, "relation_count": 0},
        ):
            assert r._detect_kg_available() is False

    def test_returns_true_when_stats_positive(self):
        r = _make_runner()
        with patch(
            "custom_app.services.kg_search.get_graph_stats",
            return_value={"entity_count": 10, "relation_count": 5},
        ):
            assert r._detect_kg_available() is True

    def test_returns_true_on_backend_error(self):
        # 后端异常时保守返回 True，避免误关 KG 工具
        r = _make_runner()
        with patch(
            "custom_app.services.kg_search.get_graph_stats",
            side_effect=RuntimeError("neo4j down"),
        ):
            assert r._detect_kg_available() is True


# ────────────────────────────────────────────────────────────────────
# system prompt 在 KG 不可用时附加提示
# ────────────────────────────────────────────────────────────────────

class TestSystemPromptKgUnavailable:
    def test_kg_unavailable_appends_note(self):
        r = _make_runner(kg_available=False)
        prompt = r._build_system_prompt(kb_name="X")
        assert "知识图谱（KG）不可用" in prompt or "KG）不可用" in prompt
        assert "query_knowledge_graph" in prompt

    def test_kg_available_no_note(self):
        r = _make_runner(kg_available=True)
        prompt = r._build_system_prompt(kb_name="X")
        assert "知识图谱（KG）不可用" not in prompt


# ────────────────────────────────────────────────────────────────────
# _synthesize_final_answer：多分支
# ────────────────────────────────────────────────────────────────────

class TestSynthesizeFinalAnswer:
    def _make(self):
        r = _make_runner(kg_available=True)
        return r

    def test_no_tool_results_returns_structured_error(self):
        r = self._make()
        out = r._synthesize_final_answer("q", messages=[
            {"role": "user", "content": "原始问题"},
        ], system_prompt="sys")
        assert out["text"] == ""
        assert out["error"] == "no_tool_result"
        assert "未收到任何工具结果" in out["error_detail"]
        assert out["tool_result_count"] == 0

    def test_empty_llm_response_marks_empty_response(self):
        r = self._make()
        with patch.object(r, "_llm_call", return_value={"text": "", "tool_calls": []}):
            out = r._synthesize_final_answer("q", messages=[
                {"role": "user", "content": "[工具结果 knowledge_search]\n命中5条..."},
            ], system_prompt="sys")
        assert out["error"] == "empty_response"
        assert out["tool_result_count"] == 1
        assert out["text"] == ""

    def test_llm_exception_marks_gemini_error(self):
        r = self._make()
        with patch.object(
            r, "_llm_call", side_effect=RuntimeError("Gemini 503")
        ):
            out = r._synthesize_final_answer("q", messages=[
                {"role": "user", "content": "[工具结果 list_knowledge_chunks]\n8条"},
            ], system_prompt="sys")
        assert out["error"] == "gemini_error"
        assert "Gemini 503" in out["error_detail"]

    def test_success_returns_text(self):
        r = self._make()
        with patch.object(
            r, "_llm_call", return_value={"text": "  最终答案  ", "tool_calls": []},
        ):
            out = r._synthesize_final_answer("q", messages=[
                {"role": "user", "content": "[工具结果 knowledge_search]\nx"},
            ], system_prompt="sys")
        assert out["text"] == "最终答案"
        assert out["error"] is None
        assert out["tool_result_count"] == 1


# ────────────────────────────────────────────────────────────────────
# _collect_tool_evidence_summary
# ────────────────────────────────────────────────────────────────────

class TestEvidenceSummary:
    def test_truncates_per_item_and_caps_total(self):
        from custom_app.services.agent_runner import AgentRunner
        msgs = [
            {"role": "user", "content": "原始问题"},
            *[
                {"role": "user", "content": f"[工具结果 t]\n" + ("a" * 500)}
                for _ in range(10)
            ],
        ]
        s = AgentRunner._collect_tool_evidence_summary(msgs, max_items=3, max_chars_per_item=50)
        assert s.count("\n") == 2  # 3 项 = 2 个换行
        # 每行不超过 max_chars + " …" + "- " 前缀
        for line in s.split("\n"):
            assert line.startswith("- ")


# ────────────────────────────────────────────────────────────────────
# 主循环耗尽轮次 + 综合失败 → 用户看到可读理由
# ────────────────────────────────────────────────────────────────────

class TestMaxIterationFallbackMessage:
    def test_synthesis_failure_emits_actionable_chunk(self):
        from custom_app.services.agent_runner import AgentRunner

        r = AgentRunner.__new__(AgentRunner)
        r.kb_id = "test_kb"
        r.max_iterations = 1  # 一轮就到顶
        r.enabled_tools = None
        r._rows = []
        r._index = MagicMock()
        r._kb_name = "测试库"
        r._adapter = MagicMock()
        r._gemini_tools = None
        r._kg_available = True
        r._registry = None  # 触发 chat_stream 内部 tools=None 路径

        # 第 1 轮：返回一个非 final_answer 的 tool_call → 工具会执行失败但有结果
        # 然后循环结束，进入 synthesize 分支
        def fake_llm(**kwargs):
            return {
                "text": "我去搜一下",
                "tool_calls": [{"name": "knowledge_search", "args": {"query": "x"}}],
            }

        with patch.object(r, "_llm_call", side_effect=fake_llm), \
             patch.object(r, "_build_system_prompt", return_value="sys"), \
             patch.object(r, "_build_initial_messages",
                          return_value=[{"role": "user", "content": "q"}]), \
             patch.object(r, "_execute_tool",
                          return_value=[{"id": "1", "doc": "D", "title": "t", "contents": "x"}]), \
             patch.object(r, "_synthesize_final_answer", return_value={
                 "text": "",
                 "tool_result_count": 1,
                 "error": "gemini_error",
                 "error_detail": "503 service unavailable",
             }):
            events = list(r.chat_stream("q"))

        # 找到最后那条 chunk（即用户能看到的最终答案）
        chunk_events = [e for e in events if e.get("type") == "chunk"]
        assert chunk_events, "至少应有一条 chunk 事件"
        final = chunk_events[-1]["content"]
        assert "未能生成最终答案" in final
        assert "503 service unavailable" in final
        assert "已检索到的证据条目数" in final
        assert "logs/app.log" in final


# ────────────────────────────────────────────────────────────────────
# logging_setup 幂等
# ────────────────────────────────────────────────────────────────────

class TestLoggingSetupIdempotent:
    def test_repeated_setup_does_not_duplicate_handlers(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ULTRARAG_LOG_DIR", str(tmp_path))
        from custom_app import logging_setup as ls

        # 强制重置内部标记（测试间隔离）
        ls._SETUP_DONE = False
        for h in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(h)

        ls.setup_logging()
        first_count = len(logging.getLogger().handlers)
        ls.setup_logging()  # 第二次调用不应增加 handler
        ls.setup_logging()
        assert len(logging.getLogger().handlers) == first_count

    def test_force_reinit_replaces_handlers(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ULTRARAG_LOG_DIR", str(tmp_path))
        from custom_app import logging_setup as ls

        ls._SETUP_DONE = False
        for h in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(h)

        ls.setup_logging()
        before = len(logging.getLogger().handlers)
        ls.setup_logging(force=True)
        after = len(logging.getLogger().handlers)
        assert before == after  # 数量稳定（清空后重挂同样的 N 个）

    def test_disable_file_env_skips_file_handlers(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ULTRARAG_LOG_DIR", str(tmp_path))
        monkeypatch.setenv("ULTRARAG_LOG_DISABLE_FILE", "1")
        from custom_app import logging_setup as ls

        ls._SETUP_DONE = False
        for h in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(h)

        ls.setup_logging(force=True)
        from logging.handlers import RotatingFileHandler
        file_handlers = [
            h for h in logging.getLogger().handlers
            if isinstance(h, RotatingFileHandler)
        ]
        assert file_handlers == []
