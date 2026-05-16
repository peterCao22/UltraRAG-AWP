"""Phase 7.1: 对话模型统一适配器 Protocol。

设计选择：内部 canonical schema 采用 **OpenAI 风格**——messages / tool_calls / role 命名
全部对齐 OpenAI Chat Completions，因为：
    1. OpenAI 是事实标准，vLLM / 多数国产模型 / Gemini 兼容端点都原生支持
    2. AgentRunner 现有 messages 拼接逻辑已经按 OpenAI 标准写（role=tool / tool_call_id /
       assistant.tool_calls）
    3. 只有 Anthropic 需要在 Adapter 内部双向转换

Adapter 职责：
    - 入口：canonical messages/tools → provider 原生格式
    - 出口：provider 响应 → canonical { text, tool_calls, finish_reason }

不在 Protocol 里的事：
    - thoughtSignature（Gemini 3 独有；通过 OpenAI 兼容端点就丢失，不强求保留）
    - safety ratings / content blocks 细节
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, Protocol, runtime_checkable


# ─── Canonical types（OpenAI 风格） ────────────────────────────────────────


@dataclass(frozen=True)
class CanonicalToolCall:
    """OpenAI 风格 tool call。"""
    id: str
    name: str
    arguments_json: str  # 模型生成的 JSON 字符串；调用方负责 json.loads


@dataclass
class CanonicalChatResponse:
    """non-streaming 调用的统一返回。

    fields:
        text:          assistant 文本输出（无 tool call 时直接答案；有 tool call 时
                       通常是空字符串或简短理由）
        tool_calls:    模型决定要调用的工具列表（一次 ReAct 回合可能多调）
        finish_reason: stop / tool_calls / length / content_filter ...
        raw:           provider 原始响应（调试用，不强制）
    """
    text: str = ""
    tool_calls: list[CanonicalToolCall] = field(default_factory=list)
    finish_reason: str = ""
    raw: Optional[dict[str, Any]] = None


@dataclass
class CanonicalStreamEvent:
    """streaming SSE 事件的统一格式。

    type:
        - "text": 文本增量（content_delta）
        - "tool_call_start": 工具调用开始（拿到 id + name）
        - "tool_call_args": 工具调用参数增量（arguments_delta，可能多次合并）
        - "tool_call_end": 工具调用结束
        - "done": 流结束（含 finish_reason）
        - "error": 错误事件
    """
    type: str
    text: str = ""
    tool_call_id: str = ""
    tool_call_name: str = ""
    arguments_delta: str = ""
    finish_reason: str = ""
    error_message: str = ""


# ─── Protocol ────────────────────────────────────────────────────────────


@runtime_checkable
class LLMAdapter(Protocol):
    """对话模型适配器统一接口。

    实现要点：
        1. call() 必须返回 CanonicalChatResponse；解析 provider 原生响应并归一化
        2. stream() 按 CanonicalStreamEvent 顺序产出；text 类型可多次出现，
           调用方累积；tool_call_start → 多个 tool_call_args → tool_call_end 是
           一个工具调用的完整生命周期
        3. tools schema 用 OpenAI 标准格式（{type: "function", function: {name,
           description, parameters}}）；Adapter 内部负责转 provider 原生格式
        4. messages 用 OpenAI 风格 role 命名（system / user / assistant / tool），
           assistant.tool_calls 用 OpenAI 格式，tool 消息用 tool_call_id 关联
    """

    def call(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Optional[list[dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> CanonicalChatResponse:
        ...

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Optional[list[dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Iterator[CanonicalStreamEvent]:
        ...

    def model_name(self) -> str:
        ...
