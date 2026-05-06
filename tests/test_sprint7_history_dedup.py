"""
Sprint 7 TDD 测试：会话历史注入 + 工具去重 + trim 精化

覆盖：
- S7-1: AgentRunner.chat_stream 接收并注入 session_history（最近 N 轮）
- S7-2: _build_initial_messages 将历史轮次放在 user 问题之前
- S7-3: 工具调用去重：同一循环内 (tool_name, args) 重复时跳过
- S7-4: chat.py 在 agent 模式下从 DB 读历史并传入 AgentRunner
- S7-5: _trim_messages_if_needed 只截断工具结果消息，不截断 user 原始问题
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# 辅助：构造无依赖的 AgentRunner（跳过 __init__）
# ─────────────────────────────────────────────────────────────────────────────

def _make_runner(max_iterations=6):
    from custom_app.services.agent_runner import AgentRunner
    r = AgentRunner.__new__(AgentRunner)
    r.kb_id = "test_kb"
    r.max_iterations = max_iterations
    r.enabled_tools = None
    r._rows = [{"id": "0", "title": "换电SOP", "contents": "打开舱门", "doc": "IFSSOP"}]
    r._index = MagicMock()
    import numpy as np
    r._index.search.return_value = (np.array([[0.9]]), np.array([[0]]))
    r._kb_name = "AGV知识库"
    r._registry = None
    r._adapter = None
    r._gemini_tools = None
    return r


# ─────────────────────────────────────────────────────────────────────────────
# S7-1 / S7-2: 会话历史注入
# ─────────────────────────────────────────────────────────────────────────────

class TestSessionHistoryInjection:
    """AgentRunner 能把历史对话轮次注入到 messages 中。"""

    def test_build_initial_messages_no_history(self):
        """无历史时，messages 只含当前 user 问题。"""
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner(kb_id="test_kb")
        msgs = r._build_initial_messages("换电步骤？", history=[])
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert "换电步骤？" in msgs[0]["content"]

    def test_build_initial_messages_with_history(self):
        """有历史时，历史 user/assistant 轮次插在当前问题之前。"""
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner(kb_id="test_kb")
        history = [
            {"role": "user", "content": "第一个问题"},
            {"role": "assistant", "content": "第一个回答"},
        ]
        msgs = r._build_initial_messages("第二个问题", history=history)
        # 顺序：历史user → 历史assistant → 当前user
        assert msgs[0]["content"] == "第一个问题"
        assert msgs[1]["content"] == "第一个回答"
        assert msgs[-1]["content"] == "第二个问题"
        assert msgs[-1]["role"] == "user"

    def test_history_limited_to_recent_6_turns(self):
        """历史超过 6 条时只取最后 6 条，防止 context 过长。"""
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner(kb_id="test_kb")
        history = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn-{i}"}
            for i in range(20)
        ]
        msgs = r._build_initial_messages("当前问题", history=history)
        # 最多 6 条历史 + 1 条当前问题
        assert len(msgs) <= 7
        # 最后一条必须是当前问题
        assert msgs[-1]["content"] == "当前问题"

    def test_history_roles_preserved(self):
        """历史消息 role 必须原样保留（user/assistant）。"""
        from custom_app.services.agent_runner import AgentRunner
        r = AgentRunner(kb_id="test_kb")
        history = [
            {"role": "user", "content": "历史问题"},
            {"role": "assistant", "content": "历史答案"},
        ]
        msgs = r._build_initial_messages("新问题", history=history)
        roles = [m["role"] for m in msgs]
        assert "user" in roles
        assert "assistant" in roles

    def test_chat_stream_accepts_history_kwarg(self):
        """chat_stream 接受 history 关键字参数，不报错。"""
        r = _make_runner(max_iterations=1)
        history = [
            {"role": "user", "content": "上轮问"},
            {"role": "assistant", "content": "上轮答"},
        ]
        with patch.object(r, "_llm_call", return_value={
            "text": "",
            "tool_calls": [{"name": "final_answer", "args": {"answer": "ok"}}],
        }), patch.object(r, "_build_system_prompt", return_value="sys"):
            events = list(r.chat_stream("新问题", history=history))

        done = [e for e in events if e.get("type") == "done"]
        assert len(done) == 1

    def test_chat_stream_history_injected_into_messages(self):
        """chat_stream 调用时，历史内容出现在传给 _llm_call 的 messages 中。"""
        r = _make_runner(max_iterations=1)
        history = [
            {"role": "user", "content": "之前的问题"},
            {"role": "assistant", "content": "之前的答案"},
        ]
        captured_messages = []

        def fake_llm(messages, system_prompt, tools=None):
            captured_messages.extend(messages)
            return {
                "text": "",
                "tool_calls": [{"name": "final_answer", "args": {"answer": "done"}}],
            }

        with patch.object(r, "_llm_call", side_effect=fake_llm), \
             patch.object(r, "_build_system_prompt", return_value="sys"):
            list(r.chat_stream("当前问题", history=history))

        contents = [m.get("content", "") for m in captured_messages]
        assert any("之前的问题" in c for c in contents)
        assert any("之前的答案" in c for c in contents)
        assert any("当前问题" in c for c in contents)


# ─────────────────────────────────────────────────────────────────────────────
# S7-3: 工具调用去重
# ─────────────────────────────────────────────────────────────────────────────

class TestToolCallDedup:
    """同一 ReAct 循环内相同 (tool_name, args) 不重复执行。"""

    def test_duplicate_tool_call_skipped(self):
        """LLM 第 1 轮和第 2 轮都要求 keyword_search("wheel")，第二次应被跳过。"""
        r = _make_runner(max_iterations=4)
        call_count = {"n": 0, "tool_exec": 0}

        def fake_llm(messages, system_prompt, tools=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"text": "第一次搜索", "tool_calls": [
                    {"name": "keyword_search", "args": {"keywords": "wheel"}},
                ]}
            if call_count["n"] == 2:
                # 重复调用同样的工具+参数
                return {"text": "再次尝试", "tool_calls": [
                    {"name": "keyword_search", "args": {"keywords": "wheel"}},
                ]}
            return {"text": "", "tool_calls": [
                {"name": "final_answer", "args": {"answer": "完成"}},
            ]}

        exec_count = {"n": 0}
        original_execute = r._execute_tool if hasattr(r, "_execute_tool") else None

        with patch.object(r, "_llm_call", side_effect=fake_llm), \
             patch.object(r, "_build_system_prompt", return_value="sys"), \
             patch.object(r, "_execute_tool", wraps=lambda name, args: exec_count.__setitem__("n", exec_count["n"] + 1) or []) as mock_exec:
            events = list(r.chat_stream("轮子问题"))

        # keyword_search("wheel") 只应被执行一次，第二次重复调用被跳过
        kw_calls = [
            c for c in mock_exec.call_args_list
            if c.args[0] == "keyword_search" and c.args[1].get("keywords") == "wheel"
        ]
        assert len(kw_calls) == 1, f"期望 keyword_search 执行 1 次，实际 {len(kw_calls)} 次"

    def test_duplicate_skip_emits_skipped_event_or_just_skips(self):
        """去重跳过时，不应 crash，done 事件必须正常发出。"""
        r = _make_runner(max_iterations=4)
        n = {"v": 0}

        def fake_llm(messages, system_prompt, tools=None):
            n["v"] += 1
            if n["v"] <= 2:
                return {"text": "", "tool_calls": [
                    {"name": "knowledge_search", "args": {"query": "重复查询"}},
                ]}
            return {"text": "", "tool_calls": [
                {"name": "final_answer", "args": {"answer": "ok"}},
            ]}

        with patch.object(r, "_llm_call", side_effect=fake_llm), \
             patch.object(r, "_build_system_prompt", return_value="sys"), \
             patch.object(r, "_execute_tool", return_value=[]):
            events = list(r.chat_stream("问"))

        assert any(e.get("type") == "done" for e in events)

    def test_different_args_not_deduped(self):
        """相同工具但不同参数，不应被去重。"""
        r = _make_runner(max_iterations=4)
        n = {"v": 0}

        def fake_llm(messages, system_prompt, tools=None):
            n["v"] += 1
            if n["v"] == 1:
                return {"text": "", "tool_calls": [
                    {"name": "keyword_search", "args": {"keywords": "电池"}},
                ]}
            if n["v"] == 2:
                return {"text": "", "tool_calls": [
                    {"name": "keyword_search", "args": {"keywords": "充电"}},
                ]}
            return {"text": "", "tool_calls": [
                {"name": "final_answer", "args": {"answer": "ok"}},
            ]}

        exec_calls = []
        with patch.object(r, "_llm_call", side_effect=fake_llm), \
             patch.object(r, "_build_system_prompt", return_value="sys"), \
             patch.object(r, "_execute_tool", side_effect=lambda n, a: exec_calls.append((n, a)) or []):
            list(r.chat_stream("问"))

        kw_calls = [c for c in exec_calls if c[0] == "keyword_search"]
        assert len(kw_calls) == 2, "不同参数的相同工具应各执行一次"


# ─────────────────────────────────────────────────────────────────────────────
# S7-5: _trim_messages_if_needed 精化
# ─────────────────────────────────────────────────────────────────────────────

class TestTrimMessagesPrecise:
    """_trim_messages_if_needed 只截断工具结果消息，保留 user 原始问题。"""

    def test_user_question_not_truncated(self):
        """user role 的原始问题不应被截断，即使超过 2000 字符。"""
        from custom_app.services.agent_runner import AgentRunner
        long_question = "问" * 3000
        messages = [{"role": "user", "content": long_question}]
        result = AgentRunner._trim_messages_if_needed(messages)
        assert result[0]["content"] == long_question

    def test_tool_result_message_truncated(self):
        """[工具结果 ...] 前缀的消息超过 2000 字符时应被截断。"""
        from custom_app.services.agent_runner import AgentRunner
        long_result = "x" * 5000
        messages = [
            {"role": "user", "content": "原始问题"},
            {"role": "user", "content": f"[工具结果 knowledge_search]\n{long_result}"},
        ]
        result = AgentRunner._trim_messages_if_needed(messages)
        assert result[0]["content"] == "原始问题"
        assert len(result[1]["content"]) < 5000
        assert "已截断" in result[1]["content"]

    def test_assistant_thought_not_truncated_if_short(self):
        """短 assistant 消息不截断。"""
        from custom_app.services.agent_runner import AgentRunner
        messages = [
            {"role": "assistant", "content": "短思考"},
            {"role": "user", "content": "[工具结果 x]\n" + "a" * 10},
        ]
        result = AgentRunner._trim_messages_if_needed(messages)
        assert result[0]["content"] == "短思考"

    def test_empty_messages_safe(self):
        """空列表不 crash。"""
        from custom_app.services.agent_runner import AgentRunner
        assert AgentRunner._trim_messages_if_needed([]) == []


# ─────────────────────────────────────────────────────────────────────────────
# S7-4: chat.py 在 agent 模式下读取并传递会话历史
# ─────────────────────────────────────────────────────────────────────────────

class TestChatApiHistoryPassing:
    """chat.py agent 模式：从 session_store 读历史并传给 AgentRunner。"""

    @pytest.fixture(autouse=True)
    def _patch_faiss(self, monkeypatch):
        """faiss 在 uv 测试环境中未安装，用空 mock 模块绕过导入。"""
        import sys
        import types
        if "faiss" not in sys.modules:
            faiss_mock = types.ModuleType("faiss")
            faiss_mock.IndexFlatIP = MagicMock()
            faiss_mock.read_index = MagicMock()
            monkeypatch.setitem(sys.modules, "faiss", faiss_mock)

    def test_agent_stream_passes_history_to_runner(self):
        """有 session_id 时，chat_stream 应读取历史并传给 AgentRunner.chat_stream。"""
        import custom_app.api.chat as chat_module

        fake_history = [
            {"role": "user", "content": "前一个问题"},
            {"role": "assistant", "content": "前一个答案"},
        ]
        captured_history = []

        class FakeAgentRunner:
            def chat_stream(self, question, *, top_k=None, profile=False, history=None):
                captured_history.extend(history or [])
                yield {"type": "chunk", "content": "答案"}
                yield {"type": "done", "answer": "答案", "meta": {}}

        with patch.object(chat_module, "_get_agent_runner", return_value=FakeAgentRunner()), \
             patch.object(chat_module, "list_messages_for_agent", return_value=fake_history):
            from custom_app.app import create_app
            client = create_app().test_client()
            resp = client.post("/api/chat/stream", json={
                "kb_id": "agv_demo",
                "question": "新问题",
                "agent_mode": "agent",
                "session_id": "sess_test_123",
            })
            _ = resp.data

        assert len(captured_history) == 2
        assert captured_history[0]["content"] == "前一个问题"
        assert captured_history[1]["content"] == "前一个答案"

    def test_list_messages_called_for_agent_mode(self):
        """agent 模式且有 session_id 时，应调用历史消息查询函数。"""
        import custom_app.api.chat as chat_module

        fake_history = [
            {"role": "user", "content": "old q"},
            {"role": "assistant", "content": "old a"},
        ]
        captured_history = []

        class FakeAgentRunner:
            def chat_stream(self, question, *, top_k=None, profile=False, history=None):
                captured_history.extend(history or [])
                yield {"type": "chunk", "content": "ok"}
                yield {"type": "done", "answer": "ok", "meta": {}}

        with patch.object(chat_module, "_get_agent_runner", return_value=FakeAgentRunner()), \
             patch.object(chat_module, "list_messages_for_agent", return_value=fake_history):
            from custom_app.app import create_app
            client = create_app().test_client()
            resp = client.post("/api/chat/stream", json={
                "kb_id": "agv_demo",
                "question": "新问题",
                "agent_mode": "agent",
                "session_id": "sess_test_123",
            })
            _ = resp.data

        assert len(captured_history) == 2
        assert captured_history[0]["content"] == "old q"
        assert captured_history[1]["content"] == "old a"
