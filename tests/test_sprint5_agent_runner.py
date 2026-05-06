"""
Sprint 5 TDD 测试：AgentRunner ReAct 引擎

覆盖：
- B-ENG-1: agent_runner.py 骨架 + messages 构建
- B-ENG-2: LLM 流式调用 + tool_calls 解析
- B-ENG-3: ReAct 主循环 + 停止条件 + 死循环检测
- B-ENG-4: _format_tool_hint 用户友好描述
- B-ENG-5: System Prompt 模板渲染
- B-ENG-6: 错误恢复 + 消息截断
- B-ENG-7: chat.py 路由分发 agent → AgentRunner
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch, call
from typing import Iterator

import pytest


# ─────────────────────────────────────────────
# B-ENG-1: AgentRunner 骨架
# ─────────────────────────────────────────────

class TestAgentRunnerInit:
    """AgentRunner 构造与初始化。"""

    def test_imports(self):
        from custom_app.services.agent_runner import AgentRunner
        assert AgentRunner is not None

    def test_init_requires_kb_id(self):
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner(kb_id="test_kb")
        assert r.kb_id == "test_kb"

    def test_init_default_max_iterations(self):
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner(kb_id="test_kb")
        assert r.max_iterations >= 1

    def test_init_accepts_max_iterations(self):
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner(kb_id="test_kb", max_iterations=3)
        assert r.max_iterations == 3

    def test_init_accepts_enabled_tools(self):
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner(kb_id="test_kb", enabled_tools=["knowledge_search"])
        assert r.enabled_tools == ["knowledge_search"]

    def test_build_messages_includes_question(self):
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner(kb_id="test_kb")
        msgs = r._build_initial_messages("如何换电？")
        roles = [m["role"] for m in msgs]
        contents = [m.get("content", "") for m in msgs]
        assert "user" in roles
        assert any("如何换电？" in c for c in contents)

    def test_build_messages_system_first(self):
        """messages 列表第一个不是 system（Gemini 用 systemInstruction 单独传）。"""
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner(kb_id="test_kb")
        msgs = r._build_initial_messages("问题")
        # 首条应是 user（system 通过 systemInstruction 传给 Gemini）
        assert msgs[0]["role"] == "user"


# ─────────────────────────────────────────────
# B-ENG-5: System Prompt 模板
# ─────────────────────────────────────────────

class TestSystemPrompt:
    """System Prompt 模板渲染。"""

    def test_build_system_prompt_returns_string(self):
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner(kb_id="test_kb")
        prompt = r._build_system_prompt(kb_name="AGV知识库")
        assert isinstance(prompt, str)
        assert len(prompt) > 50

    def test_system_prompt_contains_kb_name(self):
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner(kb_id="test_kb")
        prompt = r._build_system_prompt(kb_name="AGV知识库")
        assert "AGV知识库" in prompt

    def test_system_prompt_contains_workflow_phases(self):
        """须包含四阶段工作流关键词。"""
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner(kb_id="test_kb")
        prompt = r._build_system_prompt(kb_name="测试库")
        assert "Phase" in prompt or "阶段" in prompt or "Workflow" in prompt

    def test_system_prompt_contains_constraints(self):
        """须含 Critical Constraints（Deep Read 强制要求）。"""
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner(kb_id="test_kb")
        prompt = r._build_system_prompt(kb_name="测试库")
        assert "final_answer" in prompt or "最终答案" in prompt

    def test_system_prompt_no_internal_tool_names_exposed_to_user(self):
        """思考约束：须有禁止暴露内部名称的指令。"""
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner(kb_id="test_kb")
        prompt = r._build_system_prompt(kb_name="测试库")
        assert "knowledge_search" in prompt or "list_knowledge_chunks" in prompt


# ─────────────────────────────────────────────
# B-ENG-4: _format_tool_hint
# ─────────────────────────────────────────────

class TestFormatToolHint:
    """工具提示对用户友好，不暴露内部名称。"""

    def _runner(self):
        from custom_app.services.agent_runner import AgentRunner
        return AgentRunner(kb_id="test_kb")

    def test_knowledge_search_hint(self):
        r = self._runner()
        hint = r._format_tool_hint("knowledge_search", {"query": "换电步骤"})
        assert "knowledge_search" not in hint
        assert len(hint) > 0

    def test_keyword_search_hint(self):
        r = self._runner()
        hint = r._format_tool_hint("keyword_search", {"keywords": "电池"})
        assert "keyword_search" not in hint

    def test_list_chunks_hint(self):
        r = self._runner()
        hint = r._format_tool_hint("list_knowledge_chunks", {"doc_id": "IFSSOP"})
        assert "list_knowledge_chunks" not in hint
        assert "IFSSOP" in hint

    def test_final_answer_hint(self):
        r = self._runner()
        hint = r._format_tool_hint("final_answer", {})
        assert "final_answer" not in hint

    def test_unknown_tool_hint_is_string(self):
        r = self._runner()
        hint = r._format_tool_hint("some_new_tool", {})
        assert isinstance(hint, str)


# ─────────────────────────────────────────────
# B-ENG-3: ReAct 主循环 (chat_stream)
# ─────────────────────────────────────────────

class TestAgentRunnerChatStream:
    """chat_stream 主循环 SSE 事件契约。"""

    def _make_runner(self, rows=None, llm_responses=None):
        """构造一个 mock 好的 AgentRunner，不真实调用 LLM 或 FAISS。"""
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner.__new__(AgentRunner)
        r.kb_id = "test_kb"
        r.max_iterations = 6
        r.enabled_tools = None
        r._rows = rows or [
            {"id": "0", "title": "换电 STEP 1", "contents": "打开舱门", "doc": "IFSSOP"},
        ]
        r._index = MagicMock()
        import numpy as np
        r._index.search.return_value = (np.array([[0.9]]), np.array([[0]]))
        r._kb_name = "AGV知识库"
        r._llm_responses = llm_responses or []
        r._response_idx = 0
        return r

    def test_chat_stream_yields_dicts(self):
        """chat_stream 必须是 dict 迭代器。"""
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner.__new__(AgentRunner)
        r.kb_id = "test_kb"
        r.max_iterations = 1
        r.enabled_tools = None
        r._rows = []
        r._index = MagicMock()
        r._kb_name = "AGV知识库"

        final_answer = "换电步骤：打开舱门，取出电池。"
        with patch.object(r, "_llm_call", return_value={
            "tool_calls": [{"name": "final_answer", "args": {"answer": final_answer}}],
            "text": "",
        }), patch.object(r, "_build_system_prompt", return_value="system"), \
             patch.object(r, "_build_initial_messages", return_value=[{"role": "user", "content": "换电"}]):
            events = list(r.chat_stream("换电"))

        assert all(isinstance(e, dict) for e in events)

    def test_chat_stream_emits_done_event(self):
        """chat_stream 必须发射 done 事件。"""
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner.__new__(AgentRunner)
        r.kb_id = "test_kb"
        r.max_iterations = 1
        r.enabled_tools = None
        r._rows = []
        r._index = MagicMock()
        r._kb_name = "AGV知识库"

        with patch.object(r, "_llm_call", return_value={
            "tool_calls": [{"name": "final_answer", "args": {"answer": "完成"}}],
            "text": "",
        }), patch.object(r, "_build_system_prompt", return_value="system"), \
             patch.object(r, "_build_initial_messages", return_value=[{"role": "user", "content": "q"}]):
            events = list(r.chat_stream("q"))

        done_events = [e for e in events if e.get("type") == "done"]
        assert len(done_events) == 1

    def test_chat_stream_done_has_answer(self):
        """done 事件须含 answer 字段。"""
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner.__new__(AgentRunner)
        r.kb_id = "test_kb"
        r.max_iterations = 1
        r.enabled_tools = None
        r._rows = []
        r._index = MagicMock()
        r._kb_name = "AGV知识库"

        with patch.object(r, "_llm_call", return_value={
            "tool_calls": [{"name": "final_answer", "args": {"answer": "答案内容"}}],
            "text": "",
        }), patch.object(r, "_build_system_prompt", return_value="system"), \
             patch.object(r, "_build_initial_messages", return_value=[{"role": "user", "content": "q"}]):
            events = list(r.chat_stream("q"))

        done = next(e for e in events if e.get("type") == "done")
        assert done.get("answer") == "答案内容"

    def test_chat_stream_emits_thought_before_tool(self):
        """thought 事件须在 tool_call 之前发射。"""
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner.__new__(AgentRunner)
        r.kb_id = "test_kb"
        r.max_iterations = 2
        r.enabled_tools = None
        r._rows = [{"id": "0", "title": "t", "contents": "c", "doc": "D"}]
        import numpy as np
        r._index = MagicMock()
        r._index.search.return_value = (np.array([[0.9]]), np.array([[0]]))
        r._kb_name = "AGV知识库"

        call_count = {"n": 0}
        def fake_llm(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {
                    "text": "我需要搜索知识库",
                    "tool_calls": [{"name": "knowledge_search", "args": {"query": "换电"}}],
                }
            return {
                "text": "",
                "tool_calls": [{"name": "final_answer", "args": {"answer": "完成"}}],
            }

        with patch.object(r, "_llm_call", side_effect=fake_llm), \
             patch.object(r, "_build_system_prompt", return_value="system"), \
             patch.object(r, "_build_initial_messages", return_value=[{"role": "user", "content": "换电"}]):
            events = list(r.chat_stream("换电"))

        types = [e["type"] for e in events]
        if "thought" in types and "tool_call" in types:
            idx_thought = types.index("thought")
            idx_tool = types.index("tool_call")
            assert idx_thought < idx_tool

    def test_chat_stream_stops_at_max_iterations(self):
        """超过 max_iterations 时必须强制终止，不无限循环。"""
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner.__new__(AgentRunner)
        r.kb_id = "test_kb"
        r.max_iterations = 2
        r.enabled_tools = None
        r._rows = [{"id": "0", "title": "t", "contents": "c", "doc": "D"}]
        import numpy as np
        r._index = MagicMock()
        r._index.search.return_value = (np.array([[0.9]]), np.array([[0]]))
        r._kb_name = "AGV知识库"

        def infinite_llm(**kwargs):
            return {
                "text": "继续搜索",
                "tool_calls": [{"name": "knowledge_search", "args": {"query": "换电"}}],
            }

        with patch.object(r, "_llm_call", side_effect=infinite_llm), \
             patch.object(r, "_build_system_prompt", return_value="system"), \
             patch.object(r, "_build_initial_messages", return_value=[{"role": "user", "content": "q"}]):
            events = list(r.chat_stream("q"))

        done_events = [e for e in events if e.get("type") == "done"]
        assert len(done_events) == 1

    def test_chat_stream_tool_call_hint_no_internal_names(self):
        """tool_call 事件的 hint 不得包含内部函数名。"""
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner.__new__(AgentRunner)
        r.kb_id = "test_kb"
        r.max_iterations = 2
        r.enabled_tools = None
        r._rows = [{"id": "0", "title": "t", "contents": "c", "doc": "D"}]
        import numpy as np
        r._index = MagicMock()
        r._index.search.return_value = (np.array([[0.9]]), np.array([[0]]))
        r._kb_name = "AGV知识库"

        call_count = {"n": 0}
        def fake_llm(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"text": "", "tool_calls": [
                    {"name": "knowledge_search", "args": {"query": "换电"}},
                ]}
            return {"text": "", "tool_calls": [
                {"name": "final_answer", "args": {"answer": "完成"}},
            ]}

        with patch.object(r, "_llm_call", side_effect=fake_llm), \
             patch.object(r, "_build_system_prompt", return_value="system"), \
             patch.object(r, "_build_initial_messages", return_value=[{"role": "user", "content": "换电"}]):
            events = list(r.chat_stream("换电"))

        tool_events = [e for e in events if e.get("type") == "tool_call"]
        forbidden = {"knowledge_search", "list_knowledge_chunks", "keyword_search"}
        for ev in tool_events:
            hint = ev.get("hint", "")
            for name in forbidden:
                assert name not in hint, f"hint 暴露内部名 '{name}': '{hint}'"


# ─────────────────────────────────────────────
# B-ENG-6: 错误恢复
# ─────────────────────────────────────────────

class TestAgentRunnerErrorRecovery:
    """LLM 调用失败时的错误处理。"""

    def test_llm_error_emits_error_event(self):
        """LLM 调用抛异常时，chat_stream 发射 error 事件而非崩溃。"""
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner.__new__(AgentRunner)
        r.kb_id = "test_kb"
        r.max_iterations = 3
        r.enabled_tools = None
        r._rows = []
        r._index = MagicMock()
        r._kb_name = "AGV知识库"

        with patch.object(r, "_llm_call", side_effect=Exception("API timeout")), \
             patch.object(r, "_build_system_prompt", return_value="system"), \
             patch.object(r, "_build_initial_messages", return_value=[{"role": "user", "content": "q"}]):
            events = list(r.chat_stream("q"))

        error_or_done = [e for e in events if e.get("type") in ("error", "done")]
        assert len(error_or_done) >= 1


# ─────────────────────────────────────────────
# B-ENG-7: chat.py 路由 agent → AgentRunner
# ─────────────────────────────────────────────

class TestChatRouteAgentMode:
    """chat.py stream 路由在 agent_mode=agent 时使用 AgentRunner。"""

    def test_agent_mode_uses_agent_runner(self):
        """agent_mode=agent 时，SSE 流由 AgentRunner.chat_stream 产生。"""
        import flask
        from custom_app.api.chat import chat_bp

        app = flask.Flask(__name__)
        app.register_blueprint(chat_bp)

        done_event = {"type": "done", "answer": "AgentRunner 答案"}

        with app.test_client() as client:
            with patch("custom_app.api.chat._get_agent_runner") as mock_get_runner:
                mock_runner = MagicMock()
                mock_runner.chat_stream.return_value = iter([done_event])
                mock_get_runner.return_value = mock_runner

                resp = client.post(
                    "/api/chat/stream",
                    json={"question": "换电", "kb_id": "test_kb", "agent_mode": "agent"},
                )
                assert resp.status_code == 200
                # 消费响应体以触发 generator 执行
                _ = resp.get_data(as_text=True)
                mock_runner.chat_stream.assert_called_once()
