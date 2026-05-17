"""Phase 7.2.A 回归：AgentRunner.chat_stream 必须按 adapter 模式选 tools shape。

bug：chat_stream() 无条件用 openai_tools_to_gemini() 扁平化 tools，
     canonical 路径（vLLM / OpenAI-compat / Anthropic）拿到扁平 schema
     → vLLM 服务端 Pydantic 拒：5 validation errors `tools[0].function Field required`。
fix：chat_stream() 应按 self._adapter_canonical 分支选 shape。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _make_runner_with_mock_adapter(canonical: bool):
    from custom_app.services.agent_runner import AgentRunner
    from custom_app.services.tools.registry import ToolRegistry
    from custom_app.services.tools.knowledge_search import KnowledgeSearchTool
    from custom_app.services.tools.final_answer import FinalAnswerTool

    ar = AgentRunner.__new__(AgentRunner)
    ar.kb_id = "test_kb"
    ar._kb_name = "test_kb"
    ar.max_iterations = 1
    ar.enabled_tools = None
    ar._kg_available = False  # 关闭 KG 避免触发 detect
    ar._agent_config = None
    ar._chat_model = None

    reg = ToolRegistry()
    reg.register(KnowledgeSearchTool(rows=[], index=None))
    reg.register(FinalAnswerTool())
    ar._registry = reg

    # mock adapter，捕获实际收到的 tools 形状
    adapter = MagicMock()
    adapter.call.return_value = MagicMock(
        text="done", tool_calls=[], finish_reason="stop"
    )
    ar._adapter = adapter
    ar._adapter_canonical = canonical
    ar._source_builder = None
    ar._id_to_row_idx = {}
    ar._rows = []
    return ar, adapter


class TestToolsShapeByAdapterMode:
    def test_canonical_adapter_gets_openai_nested_schema(self):
        """Phase 7.1 canonical 模式（OpenAI / vLLM / Anthropic）应拿到嵌套 OpenAI 标准。"""
        ar, adapter = _make_runner_with_mock_adapter(canonical=True)

        # 跑一轮 chat_stream（max_iterations=1，立刻退出）
        events = list(ar.chat_stream("test question", history=[]))

        # 取 _invoke_llm 的调用参数
        assert adapter.call.called, "adapter.call should be invoked once"
        call_kwargs = adapter.call.call_args.kwargs
        tools = call_kwargs.get("tools")
        assert tools is not None, "tools should be passed"
        assert isinstance(tools, list) and tools, "tools should be non-empty list"

        # canonical 路径 → 每条都是 {type:'function', function:{name,...}}
        first = tools[0]
        assert first.get("type") == "function", (
            f"canonical adapter expects nested OpenAI schema, got flat: {first}"
        )
        assert "function" in first, (
            f"canonical adapter expects 'function' wrapper, got: {first}"
        )
        assert first["function"].get("name") == "knowledge_search"

    def test_legacy_gemini_adapter_gets_flat_schema(self):
        """老 Gemini 原生路径仍需扁平化（functionDeclarations 风格）。"""
        ar, adapter = _make_runner_with_mock_adapter(canonical=False)
        list(ar.chat_stream("test question", history=[]))

        call_kwargs = adapter.call.call_args.kwargs
        tools = call_kwargs.get("tools")
        assert tools is not None and tools
        first = tools[0]
        # Gemini 扁平：顶层就是 name / description / parameters
        assert "type" not in first or first.get("type") != "function", (
            f"legacy Gemini path expects flat schema, got: {first}"
        )
        assert first.get("name") == "knowledge_search"
