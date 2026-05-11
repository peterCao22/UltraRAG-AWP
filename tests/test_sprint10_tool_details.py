"""
Sprint 10 TDD 测试：tool_result 事件携带 details 字段

覆盖：
- S10-1: AgentRunner.chat_stream 的 tool_result 事件含 details（工具原始结果，截断）
- S10-2: details 不超过 MAX_DETAILS_CHARS（默认 1500），超长追加 "…（已截断）"
- S10-3: chat.py 的 _compact_reasoning_event 保留 details 字段
- S10-4: tool_result 失败时（dict with "error"）details 也带上 error 文本
- S10-5: final_answer 工具不发 details（answer 已经是正文，避免冗余）
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def _make_runner(max_iterations=3):
    from custom_app.services.agent_runner import AgentRunner
    r = AgentRunner.__new__(AgentRunner)
    r.kb_id = "test_kb"
    r.max_iterations = max_iterations
    r.enabled_tools = None
    r._rows = [{"id": "0", "title": "t", "contents": "c", "doc": "D"}]
    r._index = MagicMock()
    import numpy as np
    r._index.search.return_value = (np.array([[0.9]]), np.array([[0]]))
    r._kb_name = "AGV知识库"
    r._registry = None
    r._adapter = None
    r._gemini_tools = None
    return r


# ─────────────────────────────────────────────────────────────────────────────
# S10-1: tool_result 事件包含 details
# ─────────────────────────────────────────────────────────────────────────────

class TestToolResultDetailsEvent:
    def test_tool_result_includes_details_field(self):
        r = _make_runner()
        n = {"v": 0}

        def fake_llm(messages, system_prompt, tools=None):
            n["v"] += 1
            if n["v"] == 1:
                return {"text": "", "tool_calls": [
                    {"name": "knowledge_search", "args": {"query": "换电"}},
                ]}
            return {"text": "", "tool_calls": [
                {"name": "final_answer", "args": {"answer": "完成"}},
            ]}

        fake_result = [
            {"id": "0", "title": "STEP 1", "contents": "打开舱门", "doc": "IFSSOP"},
        ]
        with patch.object(r, "_llm_call", side_effect=fake_llm), \
             patch.object(r, "_build_system_prompt", return_value="sys"), \
             patch.object(r, "_build_initial_messages", return_value=[{"role": "user", "content": "q"}]), \
             patch.object(r, "_execute_tool", return_value=fake_result):
            events = list(r.chat_stream("q"))

        tool_results = [e for e in events if e.get("type") == "tool_result"
                        and e.get("tool_name") == "knowledge_search"]
        assert len(tool_results) == 1
        assert "details" in tool_results[0]
        details = tool_results[0]["details"]
        assert isinstance(details, str)
        # 内容能反序列化或至少包含原始字段
        assert "STEP 1" in details or "打开舱门" in details

    def test_details_truncates_long_results(self):
        """超过 MAX_DETAILS_CHARS 时截断并加省略提示。"""
        r = _make_runner()
        long_result = [{"id": str(i), "contents": "x" * 200, "doc": "D"} for i in range(50)]
        n = {"v": 0}

        def fake_llm(*a, **kw):
            n["v"] += 1
            if n["v"] == 1:
                return {"text": "", "tool_calls": [
                    {"name": "knowledge_search", "args": {"query": "q"}},
                ]}
            return {"text": "", "tool_calls": [
                {"name": "final_answer", "args": {"answer": "ok"}},
            ]}

        with patch.object(r, "_llm_call", side_effect=fake_llm), \
             patch.object(r, "_build_system_prompt", return_value="sys"), \
             patch.object(r, "_build_initial_messages", return_value=[{"role": "user", "content": "q"}]), \
             patch.object(r, "_execute_tool", return_value=long_result):
            events = list(r.chat_stream("q"))

        details = next(e for e in events if e.get("type") == "tool_result"
                       and e.get("tool_name") == "knowledge_search")["details"]
        assert len(details) <= 1700  # 1500 + 余量给省略提示
        assert "已截断" in details

    def test_error_result_details_contain_error_message(self):
        """工具返回 dict with error 时，details 应含错误文案。"""
        r = _make_runner()
        n = {"v": 0}

        def fake_llm(*a, **kw):
            n["v"] += 1
            if n["v"] == 1:
                return {"text": "", "tool_calls": [
                    {"name": "knowledge_search", "args": {"query": "q"}},
                ]}
            return {"text": "", "tool_calls": [
                {"name": "final_answer", "args": {"answer": "ok"}},
            ]}

        with patch.object(r, "_llm_call", side_effect=fake_llm), \
             patch.object(r, "_build_system_prompt", return_value="sys"), \
             patch.object(r, "_build_initial_messages", return_value=[{"role": "user", "content": "q"}]), \
             patch.object(r, "_execute_tool", return_value={"error": "Gemini quota exhausted"}):
            events = list(r.chat_stream("q"))

        tr = next(e for e in events if e.get("type") == "tool_result"
                  and e.get("tool_name") == "knowledge_search")
        assert "Gemini quota exhausted" in (tr.get("details") or "")

    def test_final_answer_tool_result_no_details(self):
        """final_answer 不应在 tool_result 里发 details（answer 本身就是正文）。"""
        r = _make_runner()
        with patch.object(r, "_llm_call", return_value={
            "text": "",
            "tool_calls": [{"name": "final_answer", "args": {"answer": "最终答案"}}],
        }), patch.object(r, "_build_system_prompt", return_value="sys"), \
             patch.object(r, "_build_initial_messages", return_value=[{"role": "user", "content": "q"}]):
            events = list(r.chat_stream("q"))

        fa = next(e for e in events if e.get("type") == "tool_result"
                  and e.get("tool_name") == "final_answer")
        # final_answer 的 tool_result 字段里不应有 details，或 details 为空字符串
        assert not fa.get("details")


# ─────────────────────────────────────────────────────────────────────────────
# S10-3: chat.py _compact_reasoning_event 保留 details 字段
# ─────────────────────────────────────────────────────────────────────────────

class TestCompactReasoningKeepsDetails:
    def test_compact_keeps_details_for_tool_result(self):
        from custom_app.api.chat import _compact_reasoning_event
        ev = {
            "type": "tool_result",
            "tool_name": "knowledge_search",
            "summary": "找到 5 个结果",
            "duration_ms": 80,
            "details": "原始返回的 chunk JSON 内容...",
        }
        out = _compact_reasoning_event(ev)
        assert out.get("details") == "原始返回的 chunk JSON 内容..."

    def test_compact_truncates_oversized_details(self):
        """落库前再做一次保护性截断（数据库不希望存超过 2000 的单条记录）。"""
        from custom_app.api.chat import _compact_reasoning_event
        ev = {
            "type": "tool_result",
            "tool_name": "knowledge_search",
            "summary": "找到",
            "details": "x" * 5000,
        }
        out = _compact_reasoning_event(ev)
        assert len(out["details"]) <= 2000

    def test_compact_no_details_for_other_event_types(self):
        """thought / tool_call 不带 details。"""
        from custom_app.api.chat import _compact_reasoning_event
        out_t = _compact_reasoning_event({"type": "thought", "content": "想法"})
        out_c = _compact_reasoning_event({"type": "tool_call", "tool_name": "x", "hint": "h"})
        assert "details" not in out_t
        assert "details" not in out_c


# ─────────────────────────────────────────────────────────────────────────────
# S10-2: chat.py SSE 流转发 details 字段（端到端冒烟）
# ─────────────────────────────────────────────────────────────────────────────

class TestChatStreamForwardsDetails:
    @pytest.fixture(autouse=True)
    def _patch_faiss(self, monkeypatch):
        import sys
        import types
        if "faiss" not in sys.modules:
            mod = types.ModuleType("faiss")
            mod.IndexFlatIP = MagicMock()
            mod.read_index = MagicMock()
            monkeypatch.setitem(sys.modules, "faiss", mod)

    def test_sse_payload_contains_details(self, monkeypatch):
        """前端能在 SSE 流里看到 tool_result.details。"""
        import custom_app.api.chat as chat_module

        class FakeAgentRunner:
            enabled_tools = None

            def chat_stream(self, question, *, top_k=None, profile=False, history=None):
                yield {
                    "type": "tool_result",
                    "tool_name": "knowledge_search",
                    "summary": "找到 1 个结果",
                    "duration_ms": 50,
                    "details": "[\n  {\"title\": \"换电STEP1\", \"contents\": \"打开舱门\"}\n]",
                }
                yield {"type": "chunk", "content": "answer"}
                yield {"type": "done", "answer": "answer", "meta": {}}

        with patch.object(chat_module, "_get_agent_runner", return_value=FakeAgentRunner()), \
             patch.object(chat_module, "list_messages_for_agent", return_value=[]), \
             patch.object(chat_module, "persist_chat_turn"), \
             patch("custom_app.services.agent_config_store.get_enabled_tools",
                   return_value=["knowledge_search", "list_knowledge_chunks", "final_answer"]):
            from custom_app.app import create_app
            client = create_app().test_client()
            resp = client.post("/api/chat/stream", json={
                "kb_id": "kb_x",
                "question": "q",
                "agent_mode": "agent",
            })
            body = resp.get_data(as_text=True)

        assert "details" in body
        assert "换电STEP1" in body
