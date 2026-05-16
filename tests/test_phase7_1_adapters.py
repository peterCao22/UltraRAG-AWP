"""Phase 7.1: OpenAICompatAdapter / AnthropicAdapter 单测。

策略：mock 掉 SDK 客户端（OpenAI / Anthropic），验证：
    - canonical messages → provider 原生格式的转换
    - tools schema 转换
    - 响应 → CanonicalChatResponse 的还原
    - streaming 事件序列
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_app.services.providers.llm_protocol import (
    CanonicalChatResponse,
    CanonicalStreamEvent,
)


# ═══════════════ OpenAICompatAdapter ═══════════════════════════════════

from custom_app.services.providers.openai_compat_adapter import OpenAICompatAdapter


class TestOpenAICompatCall:
    def _make_mock_response(self, content="hello", tool_calls=None):
        msg = SimpleNamespace(content=content, tool_calls=tool_calls)
        choice = SimpleNamespace(message=msg, finish_reason="stop")
        return SimpleNamespace(choices=[choice])

    def test_basic_call(self):
        adapter = OpenAICompatAdapter(api_key="k", model="gpt-4o", base_url="https://x")
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._make_mock_response("hi there")
        with patch.object(adapter, "_client", return_value=mock_client):
            resp = adapter.call([{"role": "user", "content": "hi"}])
        assert isinstance(resp, CanonicalChatResponse)
        assert resp.text == "hi there"
        assert resp.finish_reason == "stop"
        assert resp.tool_calls == []

    def test_system_prompt_prepended(self):
        adapter = OpenAICompatAdapter(api_key="k", model="m")
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._make_mock_response("ok")
        with patch.object(adapter, "_client", return_value=mock_client):
            adapter.call(
                [{"role": "user", "content": "q"}],
                system_prompt="You are X",
            )
        args = mock_client.chat.completions.create.call_args.kwargs
        assert args["messages"][0]["role"] == "system"
        assert args["messages"][0]["content"] == "You are X"

    def test_tool_calls_parsed(self):
        tc = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(name="search", arguments='{"q":"foo"}'),
        )
        adapter = OpenAICompatAdapter(api_key="k", model="m")
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._make_mock_response(
            content=None, tool_calls=[tc],
        )
        with patch.object(adapter, "_client", return_value=mock_client):
            resp = adapter.call(
                [{"role": "user", "content": "q"}],
                tools=[{"type": "function", "function": {"name": "search", "parameters": {}}}],
            )
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "search"
        assert resp.tool_calls[0].arguments_json == '{"q":"foo"}'


class TestOpenAICompatBuildMessages:
    """规整 AgentRunner 内部 dict → OpenAI 标准 tool_calls 格式。"""

    def test_simplified_tool_calls_converted_to_openai_standard(self):
        messages = [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "thinking",
                "tool_calls": [
                    {"name": "search", "args": {"q": "foo"}},  # AgentRunner 简化格式
                ],
            },
            {"role": "tool", "name": "search", "content": [{"id": "c1", "text": "result"}]},
        ]
        out = OpenAICompatAdapter._build_messages(messages, system_prompt=None)
        # assistant.tool_calls 规整成 OpenAI 标准
        assist = next(m for m in out if m["role"] == "assistant")
        assert assist["tool_calls"][0]["type"] == "function"
        assert assist["tool_calls"][0]["function"]["name"] == "search"
        assert "id" in assist["tool_calls"][0]
        # tool 消息补 tool_call_id（与 assistant 同 name 关联）
        tool_msg = next(m for m in out if m["role"] == "tool")
        assert tool_msg["tool_call_id"] == assist["tool_calls"][0]["id"]
        assert isinstance(tool_msg["content"], str)  # 序列化后


# ═══════════════ AnthropicAdapter ═══════════════════════════════════════

from custom_app.services.providers.anthropic_adapter import AnthropicAdapter


class TestAnthropicConvertMessages:
    def test_system_extracted_from_messages(self):
        messages = [
            {"role": "system", "content": "Be helpful"},
            {"role": "user", "content": "hi"},
        ]
        an_msgs, sys = AnthropicAdapter._convert_messages(messages, None)
        assert "Be helpful" in sys
        assert all(m["role"] != "system" for m in an_msgs)
        assert an_msgs[0] == {"role": "user", "content": "hi"}

    def test_system_prompt_param_merged(self):
        an_msgs, sys = AnthropicAdapter._convert_messages(
            [{"role": "user", "content": "hi"}],
            "You are X",
        )
        assert sys == "You are X"

    def test_tool_role_converted_to_user_tool_result(self):
        messages = [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "abc", "name": "search", "args": {"q": "x"}}],
            },
            {"role": "tool", "tool_call_id": "abc", "content": {"hits": 3}},
        ]
        an_msgs, _ = AnthropicAdapter._convert_messages(messages, None)
        # 最后一条应该是 user + tool_result block
        assert an_msgs[-1]["role"] == "user"
        block = an_msgs[-1]["content"][0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "abc"

    def test_assistant_tool_calls_becomes_tool_use_blocks(self):
        messages = [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "thinking out loud",
                "tool_calls": [{"id": "abc", "name": "search", "args": {"q": "x"}}],
            },
        ]
        an_msgs, _ = AnthropicAdapter._convert_messages(messages, None)
        assist = an_msgs[-1]
        assert assist["role"] == "assistant"
        # 文本块 + tool_use 块
        types = [b["type"] for b in assist["content"]]
        assert "text" in types
        assert "tool_use" in types
        tool_use = next(b for b in assist["content"] if b["type"] == "tool_use")
        assert tool_use["name"] == "search"
        assert tool_use["input"] == {"q": "x"}

    def test_tool_call_id_inferred_when_missing(self):
        """AgentRunner 简化格式没 tool_call_id；anthropic 应能从 name 推回。"""
        messages = [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"name": "search", "args": {"q": "x"}}],  # 无 id
            },
            {"role": "tool", "name": "search", "content": "result"},  # 无 tool_call_id
        ]
        an_msgs, _ = AnthropicAdapter._convert_messages(messages, None)
        # 最后 tool_result 的 tool_use_id 应该跟 assistant 自动生成的 id 一致
        assist = an_msgs[-2]
        tool_use_id = next(b for b in assist["content"] if b["type"] == "tool_use")["id"]
        tr = an_msgs[-1]["content"][0]
        assert tr["tool_use_id"] == tool_use_id


class TestAnthropicConvertTools:
    def test_openai_schema_converted(self):
        tools = [{
            "type": "function",
            "function": {
                "name": "search",
                "description": "Search docs",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }]
        out = AnthropicAdapter._convert_tools(tools)
        assert out[0]["name"] == "search"
        assert out[0]["description"] == "Search docs"
        assert "input_schema" in out[0]
        assert out[0]["input_schema"]["type"] == "object"

    def test_none_returns_none(self):
        assert AnthropicAdapter._convert_tools(None) is None
        assert AnthropicAdapter._convert_tools([]) is None


class TestAnthropicCall:
    def test_no_temperature_by_default(self):
        adapter = AnthropicAdapter(api_key="k", model="claude-opus-4")
        # mock 整个 client.messages.create
        mock_resp = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            stop_reason="end_turn",
            model="claude-opus-4",
        )
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        with patch.object(adapter, "_client", return_value=mock_client):
            adapter.call([{"role": "user", "content": "ping"}])
        args = mock_client.messages.create.call_args.kwargs
        # 新模型已弃用 temperature；不主动传
        assert "temperature" not in args

    def test_tool_use_parsed(self):
        adapter = AnthropicAdapter(api_key="k", model="claude-opus-4")
        mock_resp = SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="let me search"),
                SimpleNamespace(
                    type="tool_use", id="tu_1", name="search",
                    input={"q": "foo"},
                ),
            ],
            stop_reason="tool_use",
            model="claude-opus-4",
        )
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        with patch.object(adapter, "_client", return_value=mock_client):
            resp = adapter.call(
                [{"role": "user", "content": "q"}],
                tools=[{"type": "function", "function": {"name": "search", "parameters": {}}}],
            )
        assert resp.text == "let me search"
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].id == "tu_1"
        assert resp.tool_calls[0].name == "search"
        assert json.loads(resp.tool_calls[0].arguments_json) == {"q": "foo"}


# ═══════════════ Factory ═══════════════════════════════════════════════

from custom_app.services.chat_adapter_factory import (
    UnsupportedProviderForChat,
    resolve_chat_adapter,
)


class TestAdapterFactory:
    def test_gemini_routes_to_openai_compat(self):
        row = {
            "provider": "gemini", "model_name": "gemini-2.5-pro",
            "api_key": "k", "base_url": "",
        }
        adapter = resolve_chat_adapter(row)
        assert isinstance(adapter, OpenAICompatAdapter)
        # base_url 自动指向 Google OpenAI 兼容端点
        assert "generativelanguage" in adapter._base_url

    def test_openai_routes_to_openai_compat(self):
        row = {
            "provider": "openai", "model_name": "gpt-4o",
            "api_key": "k", "base_url": "",
        }
        adapter = resolve_chat_adapter(row)
        assert isinstance(adapter, OpenAICompatAdapter)
        assert "api.openai.com" in adapter._base_url

    def test_vllm_uses_user_provided_url(self):
        row = {
            "provider": "openai_compatible", "model_name": "Qwen2.5",
            "api_key": "", "base_url": "http://192.168.8.40:8000/v1",
        }
        adapter = resolve_chat_adapter(row)
        assert isinstance(adapter, OpenAICompatAdapter)
        assert adapter._base_url == "http://192.168.8.40:8000/v1"

    def test_anthropic_routes_to_anthropic_adapter(self):
        row = {
            "provider": "anthropic", "model_name": "claude-opus-4",
            "api_key": "k", "base_url": "",
        }
        adapter = resolve_chat_adapter(row)
        assert isinstance(adapter, AnthropicAdapter)

    def test_unknown_provider_raises(self):
        with pytest.raises(UnsupportedProviderForChat):
            resolve_chat_adapter({"provider": "bogus"})
