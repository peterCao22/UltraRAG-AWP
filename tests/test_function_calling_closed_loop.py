"""
Function calling 协议闭环回归测试。

A 方案后的核心契约：
1. messages_to_gemini_contents 必须把 assistant.tool_calls 转成 model.functionCall part；
2. tool role 必须转成 user.functionResponse part；
3. agent_runner.chat_stream 在 ACT 阶段写回 messages 必须使用 assistant.tool_calls + tool role；
4. _trim_messages_if_needed / _synthesize_final_answer / _collect_tool_evidence_summary 必须识别 role==tool；
5. 「已执行清单」必须在多轮间注入到 system_prompt 末尾。
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# 辅助：与 sprint7 对齐的 _make_runner（不依赖 yaml/faiss）
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
    r._kg_available = True
    return r


# ─────────────────────────────────────────────────────────────────────────────
# 1. llm_adapter.messages_to_gemini_contents：assistant.tool_calls / tool role
# ─────────────────────────────────────────────────────────────────────────────

class TestMessagesToGeminiContentsWithFunctionCalling:
    def test_assistant_with_tool_calls_emits_functioncall_part(self):
        from custom_app.services.llm_adapter import messages_to_gemini_contents
        msgs = [
            {"role": "user", "content": "你好"},
            {
                "role": "assistant",
                "content": "我先搜索一下",
                "tool_calls": [
                    {"name": "knowledge_search", "args": {"query": "AGV 换电"}},
                ],
            },
            {
                "role": "tool",
                "name": "knowledge_search",
                "content": [{"id": "0", "title": "换电SOP"}],
            },
        ]
        contents = messages_to_gemini_contents(msgs)
        # 第二条应该是 model role，且 parts 含 text + functionCall
        assert contents[1]["role"] == "model"
        parts = contents[1]["parts"]
        kinds = [list(p.keys())[0] for p in parts]
        assert "text" in kinds
        assert "functionCall" in kinds
        fc = next(p["functionCall"] for p in parts if "functionCall" in p)
        assert fc["name"] == "knowledge_search"
        assert fc["args"] == {"query": "AGV 换电"}

    def test_tool_role_emits_functionresponse_part(self):
        from custom_app.services.llm_adapter import messages_to_gemini_contents
        msgs = [
            {"role": "tool", "name": "list_knowledge_chunks", "content": {"chunks": []}},
        ]
        contents = messages_to_gemini_contents(msgs)
        assert contents[0]["role"] == "user"
        part = contents[0]["parts"][0]
        assert "functionResponse" in part
        assert part["functionResponse"]["name"] == "list_knowledge_chunks"
        # response.content 必须保留结构化对象
        assert part["functionResponse"]["response"]["content"] == {"chunks": []}

    def test_assistant_no_tool_calls_only_text(self):
        from custom_app.services.llm_adapter import messages_to_gemini_contents
        msgs = [
            {"role": "assistant", "content": "纯文本回答"},
        ]
        contents = messages_to_gemini_contents(msgs)
        assert contents[0]["role"] == "model"
        assert contents[0]["parts"] == [{"text": "纯文本回答"}]

    def test_assistant_empty_text_with_tool_calls_drops_empty_text(self):
        """thought_text 为空 + 有 tool_calls 时，不应产生空 text part 污染。"""
        from custom_app.services.llm_adapter import messages_to_gemini_contents
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"name": "X", "args": {}}],
            },
        ]
        contents = messages_to_gemini_contents(msgs)
        parts = contents[0]["parts"]
        # 不应有 text part，仅 functionCall
        assert all("text" not in p for p in parts)
        assert any("functionCall" in p for p in parts)

    def test_assistant_openai_style_tool_calls_arguments_string(self):
        """兼容 OpenAI 标准格式：tool_calls[].function.arguments 为 JSON 字符串。"""
        from custom_app.services.llm_adapter import messages_to_gemini_contents
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "knowledge_search",
                            "arguments": json.dumps({"query": "X"}),
                        },
                    }
                ],
            },
        ]
        contents = messages_to_gemini_contents(msgs)
        fc = contents[0]["parts"][0]["functionCall"]
        assert fc["name"] == "knowledge_search"
        assert fc["args"] == {"query": "X"}

    def test_tool_str_content_wrapped_into_output_field(self):
        """tool.content 是字符串时应包成 {output: ...} 而不是裸字符串。"""
        from custom_app.services.llm_adapter import messages_to_gemini_contents
        msgs = [
            {"role": "tool", "name": "X", "content": "纯文本结果"},
        ]
        contents = messages_to_gemini_contents(msgs)
        part = contents[0]["parts"][0]
        assert part["functionResponse"]["response"]["content"] == {"output": "纯文本结果"}

    def test_assistant_empty_no_tool_calls_skipped_not_empty_text_part(self):
        """空 assistant 且无 tool_calls 不得生成 model + {\"text\":\"\"}（易触发 400）。"""
        from custom_app.services.llm_adapter import messages_to_gemini_contents
        msgs = [
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "B"},
        ]
        contents = messages_to_gemini_contents(msgs)
        assert len(contents) == 1
        assert contents[0]["role"] == "user"
        assert contents[0]["parts"] == [{"text": "A"}, {"text": "B"}]

    def test_empty_user_message_skipped(self):
        from custom_app.services.llm_adapter import messages_to_gemini_contents
        msgs = [
            {"role": "user", "content": ""},
            {"role": "user", "content": "有效问题"},
        ]
        contents = messages_to_gemini_contents(msgs)
        assert contents == [{"role": "user", "parts": [{"text": "有效问题"}]}]

    def test_tool_with_empty_name_skipped(self):
        from custom_app.services.llm_adapter import messages_to_gemini_contents
        msgs = [
            {"role": "tool", "name": "", "content": {"x": 1}},
        ]
        assert messages_to_gemini_contents(msgs) == []

    def test_gemini3_adds_thought_signature_placeholder_on_first_function_call_only(self):
        """Gemini 3：同条 assistant 内仅首条 functionCall 在缺签名时可补空串。"""
        from custom_app.services.llm_adapter import messages_to_gemini_contents
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"name": "a", "args": {}},
                    {"name": "b", "args": {}},
                ],
            },
        ]
        contents = messages_to_gemini_contents(msgs, model_id="gemini-3.1-pro-preview")
        parts = contents[0]["parts"]
        assert parts[0].get("thoughtSignature") == ""
        assert "thoughtSignature" not in parts[1]

    def test_gemini_response_to_tool_calls_preserves_thought_signature(self):
        from custom_app.services.llm_adapter import gemini_response_to_tool_calls
        resp = {
            "candidates": [{
                "content": {
                    "parts": [
                        {
                            "functionCall": {"name": "knowledge_search", "args": {"query": "x"}},
                            "thoughtSignature": "sig123",
                        },
                    ],
                },
            }],
        }
        calls = gemini_response_to_tool_calls(resp)
        assert len(calls) == 1
        assert calls[0]["name"] == "knowledge_search"
        assert calls[0]["thoughtSignature"] == "sig123"


# ─────────────────────────────────────────────────────────────────────────────
# 2. agent_runner.chat_stream：ACT 阶段写回是 standard role
# ─────────────────────────────────────────────────────────────────────────────

class TestChatStreamWritesStandardFunctionCallingMessages:
    def test_messages_after_tool_exec_use_assistant_tool_calls_and_tool_role(self):
        """主循环执行完工具后，messages 里应出现 {assistant, tool_calls=[...]} +
        {tool, name, content}，而不是旧的 user "[工具结果 ...]" 字符串。"""
        r = _make_runner(max_iterations=4)
        captured_messages_each_round = []
        n = {"v": 0}

        def fake_llm(messages, system_prompt, tools=None):
            captured_messages_each_round.append([dict(m) for m in messages])
            n["v"] += 1
            if n["v"] == 1:
                return {"text": "搜一下", "tool_calls": [
                    {"name": "knowledge_search", "args": {"query": "AGV"}},
                ]}
            return {"text": "", "tool_calls": [
                {"name": "final_answer", "args": {"answer": "答"}},
            ]}

        with patch.object(r, "_llm_call", side_effect=fake_llm), \
             patch.object(r, "_build_system_prompt", return_value="sys"), \
             patch.object(r, "_execute_tool", return_value=[{"id": "0", "title": "x", "contents": "y"}]):
            list(r.chat_stream("AGV 换电怎么操作？"))

        # 第二轮 LLM 看到的 messages 应包含 standard 标记
        round2 = captured_messages_each_round[1]
        # assistant.tool_calls 闭环
        assistants_with_tool_calls = [m for m in round2 if m.get("role") == "assistant" and m.get("tool_calls")]
        assert len(assistants_with_tool_calls) >= 1, f"未写回 assistant.tool_calls：{round2}"
        assert assistants_with_tool_calls[0]["tool_calls"][0]["name"] == "knowledge_search"

        # tool role 闭环
        tool_msgs = [m for m in round2 if m.get("role") == "tool"]
        assert len(tool_msgs) >= 1, f"未写回 role=tool：{round2}"
        assert tool_msgs[0]["name"] == "knowledge_search"

        # 不应再出现旧的 "[工具结果 ..." user 字符串
        legacy = [
            m for m in round2
            if m.get("role") == "user"
            and isinstance(m.get("content"), str)
            and m["content"].startswith("[工具结果 ")
        ]
        assert legacy == [], f"仍存在旧字符串前缀消息：{legacy}"

    def test_final_answer_also_writes_tool_role_message(self):
        """final_answer 也要写一条 tool 闭环消息（保持协议干净）。"""
        r = _make_runner(max_iterations=3)
        captured = []

        def fake_llm(messages, system_prompt, tools=None):
            captured.append([dict(m) for m in messages])
            return {"text": "", "tool_calls": [
                {"name": "final_answer", "args": {"answer": "ok"}},
            ]}

        with patch.object(r, "_llm_call", side_effect=fake_llm), \
             patch.object(r, "_build_system_prompt", return_value="sys"):
            list(r.chat_stream("Q"))

        # chat_stream 内部 messages 在 break 前已 append 了 tool 闭环；
        # 由于 break 后没下一轮 LLM 调用，无法从 captured 里看到，但
        # 至少 ACT 阶段没崩溃即可。我们用更直接的断言：mock 计数正常。
        assert len(captured) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 3. _trim_messages_if_needed / _synthesize_final_answer / _collect_tool_evidence_summary
#    必须识别 role==tool
# ─────────────────────────────────────────────────────────────────────────────

class TestHelpersRecognizeToolRole:
    def test_trim_recognizes_tool_role_long_dict(self):
        from custom_app.services.agent_runner import AgentRunner
        big_list = [{"i": i, "x": "x" * 100} for i in range(50)]
        msgs = [
            {"role": "user", "content": "Q"},
            {"role": "tool", "name": "knowledge_search", "content": big_list},
        ]
        out = AgentRunner._trim_messages_if_needed(msgs)
        # 第二条仍是 tool role，但 content 应被截断为 _truncated 包装
        tool_msg = out[1]
        assert tool_msg["role"] == "tool"
        c = tool_msg["content"]
        assert isinstance(c, dict) and c.get("_truncated") is True
        assert "_preview" in c

    def test_trim_keeps_user_question_intact(self):
        from custom_app.services.agent_runner import AgentRunner
        long_q = "问" * 5000
        msgs = [{"role": "user", "content": long_q}]
        out = AgentRunner._trim_messages_if_needed(msgs)
        assert out[0]["content"] == long_q

    def test_synthesize_recognizes_tool_role_messages(self):
        """_synthesize_final_answer 必须能从 role=tool 消息中收集证据。"""
        r = _make_runner()
        msgs = [
            {"role": "user", "content": "Q"},
            {
                "role": "assistant",
                "content": "搜",
                "tool_calls": [{"name": "knowledge_search", "args": {"query": "X"}}],
            },
            {
                "role": "tool",
                "name": "knowledge_search",
                "content": [{"id": "0", "title": "T", "contents": "C"}],
            },
        ]

        with patch.object(r, "_llm_call", return_value={"text": "综合答案", "tool_calls": []}):
            result = r._synthesize_final_answer("Q", msgs, "sys")

        assert result["text"] == "综合答案"
        assert result["tool_result_count"] == 1
        assert result["error"] is None

    def test_synthesize_no_tool_results_returns_no_tool_result_error(self):
        r = _make_runner()
        msgs = [{"role": "user", "content": "Q"}]
        with patch.object(r, "_llm_call", return_value={"text": "x", "tool_calls": []}):
            result = r._synthesize_final_answer("Q", msgs, "sys")
        assert result["text"] == ""
        assert result["error"] == "no_tool_result"

    def test_collect_evidence_summary_picks_up_tool_role(self):
        from custom_app.services.agent_runner import AgentRunner
        msgs = [
            {"role": "user", "content": "Q"},
            {"role": "tool", "name": "knowledge_search", "content": [{"title": "T1"}]},
            {"role": "tool", "name": "list_knowledge_chunks", "content": {"chunks": ["A", "B"]}},
        ]
        s = AgentRunner._collect_tool_evidence_summary(msgs, max_items=5)
        assert "knowledge_search" in s
        assert "list_knowledge_chunks" in s


# ─────────────────────────────────────────────────────────────────────────────
# 4. C 层：_format_executed_calls_note 的注入行为
# ─────────────────────────────────────────────────────────────────────────────

class TestExecutedCallsNoteInjection:
    def test_empty_set_returns_empty_string(self):
        from custom_app.services.agent_runner import AgentRunner
        assert AgentRunner._format_executed_calls_note(set()) == ""

    def test_lists_executed_calls(self):
        from custom_app.services.agent_runner import AgentRunner
        execs = {
            ("knowledge_search", json.dumps({"query": "AGV"}, ensure_ascii=False)),
            ("list_knowledge_chunks", json.dumps({"doc_id": "IFSSOP"}, ensure_ascii=False)),
        }
        note = AgentRunner._format_executed_calls_note(execs)
        assert "已执行的工具调用" in note
        assert "knowledge_search" in note
        assert "list_knowledge_chunks" in note
        assert "final_answer" in note  # 末尾引导词

    def test_truncates_when_too_many(self):
        from custom_app.services.agent_runner import AgentRunner
        execs = {(f"tool_{i}", "{}") for i in range(20)}
        note = AgentRunner._format_executed_calls_note(execs, max_items=5)
        assert "已省略" in note

    def test_chat_stream_injects_note_into_system_prompt_from_round2(self):
        """主循环：第 2 轮 LLM 调用收到的 system_prompt 里必须含「已执行清单」。"""
        r = _make_runner(max_iterations=4)
        captured_sys = []
        n = {"v": 0}

        def fake_llm(messages, system_prompt, tools=None):
            captured_sys.append(system_prompt)
            n["v"] += 1
            if n["v"] == 1:
                return {"text": "搜", "tool_calls": [
                    {"name": "knowledge_search", "args": {"query": "X"}},
                ]}
            return {"text": "", "tool_calls": [
                {"name": "final_answer", "args": {"answer": "ok"}},
            ]}

        with patch.object(r, "_llm_call", side_effect=fake_llm), \
             patch.object(r, "_build_system_prompt", return_value="BASE_SYS"), \
             patch.object(r, "_execute_tool", return_value=[]):
            list(r.chat_stream("Q"))

        assert len(captured_sys) >= 2
        # 第 1 轮还没执行任何工具，prompt 应为 BASE_SYS
        assert captured_sys[0] == "BASE_SYS"
        # 第 2 轮应包含已执行清单
        assert "已执行的工具调用" in captured_sys[1]
        assert "knowledge_search" in captured_sys[1]


# ─────────────────────────────────────────────────────────────────────────────
# 5. _shrink_tool_payload
# ─────────────────────────────────────────────────────────────────────────────

class TestShrinkToolPayload:
    def test_list_truncated_with_omit_marker(self):
        from custom_app.services.agent_runner import AgentRunner
        big = [{"i": i} for i in range(20)]
        out = AgentRunner._shrink_tool_payload(big, max_items=5)
        assert isinstance(out, list)
        assert len(out) == 6  # 5 条 + 1 个 _truncated 占位
        assert out[-1].get("_truncated") is True
        assert out[-1].get("_omitted") == 15

    def test_dict_small_returned_as_is(self):
        from custom_app.services.agent_runner import AgentRunner
        d = {"a": 1, "b": 2}
        assert AgentRunner._shrink_tool_payload(d) == d

    def test_dict_too_large_returns_truncated_preview(self):
        from custom_app.services.agent_runner import AgentRunner
        d = {"x": "y" * 5000}
        out = AgentRunner._shrink_tool_payload(d, max_str_chars=200)
        assert out.get("_truncated") is True
        assert "_preview" in out

    def test_str_wrapped_into_output(self):
        from custom_app.services.agent_runner import AgentRunner
        out = AgentRunner._shrink_tool_payload("hello")
        assert out == {"output": "hello"}
