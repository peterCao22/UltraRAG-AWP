"""
Sprint 4 TDD 测试：工具系统 + Gemini LLM 适配器

覆盖：
- registry.py: ToolRegistry 注册/查询/enabled_tools 过滤
- knowledge_search.py: 语义搜索工具
- keyword_search.py: 关键词匹配工具
- list_chunks.py: 全文分块阅读工具
- final_answer.py: 最终答案工具
- llm_adapter.py: Gemini function calling 格式转换
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────
# B-TOOL-1: ToolRegistry
# ─────────────────────────────────────────────

class TestToolRegistry:
    """registry.py: ToolRegistry 注册/查询/过滤。"""

    def test_registry_imports(self):
        from custom_app.services.tools.registry import ToolRegistry
        assert ToolRegistry is not None

    def test_register_and_get(self):
        from custom_app.services.tools.registry import ToolRegistry
        reg = ToolRegistry()
        tool = MagicMock()
        tool.name = "test_tool"
        reg.register(tool)
        assert reg.get("test_tool") is tool

    def test_get_unknown_returns_none(self):
        from custom_app.services.tools.registry import ToolRegistry
        reg = ToolRegistry()
        assert reg.get("nonexistent") is None

    def test_list_all(self):
        from custom_app.services.tools.registry import ToolRegistry
        reg = ToolRegistry()
        t1, t2 = MagicMock(), MagicMock()
        t1.name, t2.name = "a", "b"
        reg.register(t1)
        reg.register(t2)
        names = [t.name for t in reg.list_all()]
        assert "a" in names and "b" in names

    def test_enabled_tools_filter(self):
        """enabled_tools 列表只保留指定工具。"""
        from custom_app.services.tools.registry import ToolRegistry
        reg = ToolRegistry()
        for n in ["knowledge_search", "keyword_search", "final_answer"]:
            t = MagicMock()
            t.name = n
            reg.register(t)
        filtered = reg.list_enabled(["knowledge_search", "final_answer"])
        names = [t.name for t in filtered]
        assert "knowledge_search" in names
        assert "final_answer" in names
        assert "keyword_search" not in names

    def test_enabled_tools_none_means_all(self):
        """enabled_tools=None 时返回所有工具。"""
        from custom_app.services.tools.registry import ToolRegistry
        reg = ToolRegistry()
        for n in ["knowledge_search", "keyword_search"]:
            t = MagicMock()
            t.name = n
            reg.register(t)
        assert len(reg.list_enabled(None)) == 2

    def test_final_answer_always_enabled(self):
        """final_answer 始终在列表中，即使 enabled_tools 未包含它。"""
        from custom_app.services.tools.registry import ToolRegistry
        reg = ToolRegistry()
        for n in ["knowledge_search", "final_answer"]:
            t = MagicMock()
            t.name = n
            reg.register(t)
        filtered = reg.list_enabled(["knowledge_search"])
        names = [t.name for t in filtered]
        assert "final_answer" in names

    def test_get_openai_schemas(self):
        """list_enabled 的工具须能导出 openai_schema。"""
        from custom_app.services.tools.registry import ToolRegistry
        reg = ToolRegistry()
        t = MagicMock()
        t.name = "knowledge_search"
        t.openai_schema = {"type": "function", "function": {"name": "knowledge_search"}}
        reg.register(t)
        schemas = reg.get_schemas(None)
        assert any(s["function"]["name"] == "knowledge_search" for s in schemas)


# ─────────────────────────────────────────────
# B-TOOL-2: KnowledgeSearchTool
# ─────────────────────────────────────────────

class TestKnowledgeSearchTool:
    """knowledge_search.py: 语义向量搜索工具。"""

    def _make_tool(self, rows=None, index=None):
        from custom_app.services.tools.knowledge_search import KnowledgeSearchTool
        rows = rows or [
            {"id": "0", "title": "换电步骤1", "contents": "打开舱门", "doc": "IFSSOP"},
            {"id": "1", "title": "换电步骤2", "contents": "取出电池", "doc": "IFSSOP"},
        ]
        mock_index = index or MagicMock()
        import numpy as np
        mock_index.search.return_value = (
            np.array([[0.9, 0.8]]),
            np.array([[0, 1]]),
        )
        return KnowledgeSearchTool(rows=rows, index=mock_index)

    def test_name_is_knowledge_search(self):
        tool = self._make_tool()
        assert tool.name == "knowledge_search"

    def test_has_openai_schema(self):
        tool = self._make_tool()
        schema = tool.openai_schema
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "knowledge_search"
        assert "query" in schema["function"]["parameters"]["properties"]

    def test_run_returns_list_of_chunks(self):
        tool = self._make_tool()
        with patch("custom_app.services.tools.knowledge_search.embed_query") as mock_embed:
            import numpy as np
            mock_embed.return_value = np.zeros(768, dtype="float32")
            result = tool.run(query="换电", top_k=2)
        assert isinstance(result, list)
        assert len(result) >= 1
        assert "title" in result[0]
        assert "contents" in result[0]

    def test_run_returns_doc_field(self):
        tool = self._make_tool()
        with patch("custom_app.services.tools.knowledge_search.embed_query") as mock_embed:
            import numpy as np
            mock_embed.return_value = np.zeros(768, dtype="float32")
            result = tool.run(query="换电", top_k=2)
        assert "doc" in result[0]

    def test_run_respects_top_k(self):
        tool = self._make_tool()
        with patch("custom_app.services.tools.knowledge_search.embed_query") as mock_embed:
            import numpy as np
            mock_embed.return_value = np.zeros(768, dtype="float32")
            result = tool.run(query="换电", top_k=1)
        assert len(result) <= 1


# ─────────────────────────────────────────────
# B-TOOL-3: KeywordSearchTool
# ─────────────────────────────────────────────

class TestKeywordSearchTool:
    """keyword_search.py: 关键词精确匹配工具。"""

    def _make_tool(self):
        from custom_app.services.tools.keyword_search import KeywordSearchTool
        rows = [
            {"id": "0", "title": "换电步骤", "contents": "打开舱门，取出旧电池", "doc": "IFSSOP"},
            {"id": "1", "title": "充电规范", "contents": "将新电池推入舱内", "doc": "IFSSOP"},
            {"id": "2", "title": "安全注意事项", "contents": "佩戴防护手套", "doc": "SAFETY"},
        ]
        return KeywordSearchTool(rows=rows)

    def test_name_is_keyword_search(self):
        tool = self._make_tool()
        assert tool.name == "keyword_search"

    def test_has_openai_schema(self):
        tool = self._make_tool()
        schema = tool.openai_schema
        assert schema["type"] == "function"
        assert "keywords" in schema["function"]["parameters"]["properties"]

    def test_matches_keyword_in_contents(self):
        tool = self._make_tool()
        result = tool.run(keywords="舱门")
        assert any("舱门" in r.get("contents", "") for r in result)

    def test_matches_keyword_in_title(self):
        tool = self._make_tool()
        result = tool.run(keywords="充电规范")
        assert any("充电" in r.get("title", "") for r in result)

    def test_no_match_returns_empty(self):
        tool = self._make_tool()
        result = tool.run(keywords="xyz_不存在的词_abc")
        assert result == []

    def test_case_insensitive(self):
        from custom_app.services.tools.keyword_search import KeywordSearchTool
        rows = [{"id": "0", "title": "SOP Manual", "contents": "battery swap", "doc": "X"}]
        tool = KeywordSearchTool(rows=rows)
        result = tool.run(keywords="BATTERY")
        assert len(result) >= 1

    def test_top_k_limits_results(self):
        tool = self._make_tool()
        result = tool.run(keywords="电池", top_k=1)
        assert len(result) <= 1


# ─────────────────────────────────────────────
# B-TOOL-4: ListChunksTool
# ─────────────────────────────────────────────

class TestListChunksTool:
    """list_chunks.py: 按 doc 返回全部 chunk（Deep Read）。"""

    def _make_tool(self):
        from custom_app.services.tools.list_chunks import ListChunksTool
        rows = [
            {"id": "0", "title": "换电 STEP 1", "contents": "打开舱门", "doc": "IFSSOP"},
            {"id": "1", "title": "换电 STEP 2", "contents": "取出电池", "doc": "IFSSOP"},
            {"id": "2", "title": "安全规范", "contents": "戴手套", "doc": "SAFETY"},
        ]
        return ListChunksTool(rows=rows)

    def test_name_is_list_knowledge_chunks(self):
        tool = self._make_tool()
        assert tool.name == "list_knowledge_chunks"

    def test_has_openai_schema(self):
        tool = self._make_tool()
        schema = tool.openai_schema
        assert schema["type"] == "function"
        assert "doc_id" in schema["function"]["parameters"]["properties"]

    def test_returns_all_chunks_for_doc(self):
        tool = self._make_tool()
        result = tool.run(doc_id="IFSSOP")
        assert len(result) == 2
        titles = [r["title"] for r in result]
        assert "换电 STEP 1" in titles
        assert "换电 STEP 2" in titles

    def test_unknown_doc_returns_empty(self):
        tool = self._make_tool()
        result = tool.run(doc_id="NOTEXIST")
        assert result == []

    def test_does_not_mix_docs(self):
        tool = self._make_tool()
        result = tool.run(doc_id="SAFETY")
        assert all(r.get("doc") == "SAFETY" for r in result)

    def test_passes_through_images_field(self):
        """images 字段必须透传，否则 Agent 在多轮回答中无法引用图片。"""
        from custom_app.services.tools.list_chunks import ListChunksTool
        rows = [
            {"id": "0", "title": "STEP 1", "contents": "打开舱门",
             "doc": "IFSSOP", "images": ["images/IFSSOP/img_0001.png"]},
            {"id": "1", "title": "STEP 2", "contents": "取出电池",
             "doc": "IFSSOP", "images": ["images/IFSSOP/img_0002.png", "images/IFSSOP/img_0003.png"]},
        ]
        tool = ListChunksTool(rows=rows)
        result = tool.run(doc_id="IFSSOP")
        assert len(result) == 2
        assert result[0]["images"] == ["images/IFSSOP/img_0001.png"]
        assert len(result[1]["images"]) == 2

    def test_missing_images_field_defaults_to_empty_list(self):
        """row 没有 images 字段时不应报错，返回空列表。"""
        from custom_app.services.tools.list_chunks import ListChunksTool
        rows = [
            {"id": "0", "title": "纯文本", "contents": "无图片", "doc": "TXT"},
        ]
        tool = ListChunksTool(rows=rows)
        result = tool.run(doc_id="TXT")
        assert result[0]["images"] == []


# ─────────────────────────────────────────────
# B-TOOL-5: FinalAnswerTool
# ─────────────────────────────────────────────

class TestFinalAnswerTool:
    """final_answer.py: 终止 ReAct 循环的工具。"""

    def test_name_is_final_answer(self):
        from custom_app.services.tools.final_answer import FinalAnswerTool
        tool = FinalAnswerTool()
        assert tool.name == "final_answer"

    def test_has_openai_schema(self):
        from custom_app.services.tools.final_answer import FinalAnswerTool
        tool = FinalAnswerTool()
        schema = tool.openai_schema
        assert schema["type"] == "function"
        assert "answer" in schema["function"]["parameters"]["properties"]

    def test_run_returns_answer(self):
        from custom_app.services.tools.final_answer import FinalAnswerTool
        tool = FinalAnswerTool()
        result = tool.run(answer="换电需要3步")
        assert result["answer"] == "换电需要3步"

    def test_run_signals_stop(self):
        """run() 结果须含 stop=True，供引擎检测循环终止。"""
        from custom_app.services.tools.final_answer import FinalAnswerTool
        tool = FinalAnswerTool()
        result = tool.run(answer="完成")
        assert result.get("stop") is True


# ─────────────────────────────────────────────
# B-TOOL-6: GeminiLLMAdapter
# ─────────────────────────────────────────────

class TestGeminiLLMAdapter:
    """llm_adapter.py: Gemini function calling 格式适配器。"""

    def _make_adapter(self):
        from custom_app.services.llm_adapter import GeminiLLMAdapter
        return GeminiLLMAdapter(api_key="test_key", model="gemini-2.0-flash")

    def test_imports(self):
        from custom_app.services.llm_adapter import GeminiLLMAdapter
        assert GeminiLLMAdapter is not None

    def test_openai_tools_to_gemini(self):
        """OpenAI tools 格式转换为 Gemini functionDeclarations 格式。"""
        from custom_app.services.llm_adapter import openai_tools_to_gemini
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": "knowledge_search",
                    "description": "搜索知识库",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "查询词"}
                        },
                        "required": ["query"],
                    },
                },
            }
        ]
        gemini_tools = openai_tools_to_gemini(openai_tools)
        assert isinstance(gemini_tools, list)
        assert len(gemini_tools) == 1
        decl = gemini_tools[0]
        assert decl["name"] == "knowledge_search"
        assert "parameters" in decl

    def test_gemini_response_to_tool_calls(self):
        """Gemini functionCall 响应解析为标准 tool_call 列表。"""
        from custom_app.services.llm_adapter import gemini_response_to_tool_calls
        gemini_response = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "functionCall": {
                            "name": "knowledge_search",
                            "args": {"query": "换电步骤"},
                        }
                    }]
                },
                "finishReason": "STOP",
            }]
        }
        tool_calls = gemini_response_to_tool_calls(gemini_response)
        assert len(tool_calls) == 1
        assert tool_calls[0]["name"] == "knowledge_search"
        assert tool_calls[0]["args"]["query"] == "换电步骤"

    def test_gemini_response_no_tool_calls(self):
        """纯文本响应（无 functionCall）返回空列表。"""
        from custom_app.services.llm_adapter import gemini_response_to_tool_calls
        gemini_response = {
            "candidates": [{
                "content": {
                    "parts": [{"text": "这是最终答案"}]
                },
                "finishReason": "STOP",
            }]
        }
        tool_calls = gemini_response_to_tool_calls(gemini_response)
        assert tool_calls == []

    def test_gemini_response_extract_text(self):
        """从 Gemini 响应提取纯文本内容。"""
        from custom_app.services.llm_adapter import gemini_response_extract_text
        gemini_response = {
            "candidates": [{
                "content": {
                    "parts": [{"text": "第一部分"}, {"text": "第二部分"}]
                }
            }]
        }
        text = gemini_response_extract_text(gemini_response)
        assert "第一部分" in text
        assert "第二部分" in text

    def test_messages_to_gemini_contents(self):
        """OpenAI messages 格式转换为 Gemini contents 格式。"""
        from custom_app.services.llm_adapter import messages_to_gemini_contents
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "您好！"},
        ]
        contents = messages_to_gemini_contents(messages)
        assert isinstance(contents, list)
        assert contents[0]["role"] == "user"
        assert contents[1]["role"] == "model"

    def test_tool_result_to_gemini_part(self):
        """工具结果转换为 Gemini functionResponse part 格式。"""
        from custom_app.services.llm_adapter import tool_result_to_gemini_part
        part = tool_result_to_gemini_part(
            tool_name="knowledge_search",
            result=[{"title": "换电步骤", "contents": "打开舱门"}],
        )
        assert part["functionResponse"]["name"] == "knowledge_search"
        assert "response" in part["functionResponse"]

    def test_adapter_build_request_body(self):
        """GeminiLLMAdapter.build_request_body 生成正确结构。"""
        adapter = self._make_adapter()
        tools_schema = [{"name": "knowledge_search", "description": "搜索", "parameters": {}}]
        messages = [{"role": "user", "content": "换电步骤"}]
        body = adapter.build_request_body(
            messages=messages,
            tools=tools_schema,
            system_prompt="你是AGV助手",
        )
        assert "contents" in body
        assert "tools" in body or "systemInstruction" in body
