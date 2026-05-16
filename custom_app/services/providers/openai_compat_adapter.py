"""Phase 7.1: OpenAI 兼容协议适配器（用于 OpenAI / vLLM / Gemini OpenAI-compat 端点）。

实现细节：
    - 用 openai SDK；lazy import 避免缺包影响其它路径
    - messages / tools schema 直接是 OpenAI 标准，无需转换
    - tool_calls 解析直接走 SDK 数据模型
    - streaming 按 SSE chunk 转 CanonicalStreamEvent；OpenAI delta 里 tool_calls
      是分段拼接的（index + arguments delta），我们按 index 维持一个进行中的
      tool call，在结束时（finish_reason=tool_calls）触发 tool_call_end
"""

from __future__ import annotations

import logging
from typing import Any, Iterator, Optional

from custom_app.services.providers.llm_protocol import (
    CanonicalChatResponse,
    CanonicalStreamEvent,
    CanonicalToolCall,
)

logger = logging.getLogger(__name__)


class OpenAIDependencyMissing(RuntimeError):
    """openai SDK 未安装。"""


class OpenAICompatAdapter:
    """适用于 OpenAI / OpenAI 兼容（vLLM、Qwen、Gemini compat 端点等）。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "",
        timeout: float = 300.0,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        self._api_key = api_key or "dummy"  # vLLM 内网常无 key，给个占位
        self._model = model
        self._base_url = (base_url or "").strip().rstrip("/")
        self._timeout = float(timeout)
        self._extra = extra or {}

    def model_name(self) -> str:
        return self._model

    def _client(self):
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as e:
            raise OpenAIDependencyMissing(
                "openai SDK not installed; run `pip install openai`"
            ) from e
        kwargs: dict[str, Any] = {"api_key": self._api_key, "timeout": self._timeout}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        return OpenAI(**kwargs)

    @staticmethod
    def _build_messages(
        messages: list[dict[str, Any]],
        system_prompt: Optional[str],
    ) -> list[dict[str, Any]]:
        """规整 AgentRunner 内部 messages → OpenAI Chat Completions 标准。

        AgentRunner 内部用简化 dict（assistant.tool_calls 是 [{id?, name, args}]；
        tool 消息有 name 但可能缺 tool_call_id）；这里把它们转成 OpenAI 标准：
            assistant.tool_calls = [{id, type:"function", function:{name, arguments}}]
            tool 消息 = {role:"tool", tool_call_id, content}
        """
        import json as _json
        import uuid as _uuid

        out: list[dict[str, Any]] = []
        if system_prompt:
            has_system = any(m.get("role") == "system" for m in messages)
            if not has_system:
                out.append({"role": "system", "content": system_prompt})

        # 记下最近一个 assistant.tool_calls 的"名→id"映射，给后续 tool 消息补 tool_call_id
        last_name_to_id: dict[str, str] = {}

        for m in messages:
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                std_tool_calls = []
                last_name_to_id = {}
                for tc in m["tool_calls"]:
                    if not isinstance(tc, dict):
                        continue
                    # 已是 OpenAI 标准 {id, type, function: {name, arguments}}
                    if tc.get("type") == "function" and tc.get("function"):
                        std_tool_calls.append(tc)
                        last_name_to_id[tc["function"].get("name", "")] = tc.get("id", "")
                        continue
                    # AgentRunner 简化格式 {name, args, id?}
                    name = tc.get("name", "")
                    args = tc.get("args") or {}
                    tc_id = tc.get("id") or f"call_{_uuid.uuid4().hex[:12]}"
                    last_name_to_id[name] = tc_id
                    std_tool_calls.append({
                        "id": tc_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": _json.dumps(args, ensure_ascii=False)
                                if not isinstance(args, str) else args,
                        },
                    })
                out.append({
                    "role": "assistant",
                    "content": m.get("content") or "",
                    "tool_calls": std_tool_calls,
                })
                continue

            if role == "tool":
                # 补 tool_call_id：优先 m["tool_call_id"]，其次按 name 查最近映射
                tc_id = m.get("tool_call_id") or last_name_to_id.get(m.get("name", ""), "")
                content = m.get("content")
                if not isinstance(content, str):
                    try:
                        content = _json.dumps(content, ensure_ascii=False)
                    except (TypeError, ValueError):
                        content = str(content)
                tool_msg: dict[str, Any] = {
                    "role": "tool",
                    "content": content,
                }
                if tc_id:
                    tool_msg["tool_call_id"] = tc_id
                if m.get("name"):
                    tool_msg["name"] = m["name"]
                out.append(tool_msg)
                continue

            # user / system / 其它 assistant（纯文本）保持不变
            out.append({
                "role": role or "user",
                "content": m.get("content") or "",
            })
        return out

    def call(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Optional[list[dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> CanonicalChatResponse:
        client = self._client()
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": self._build_messages(messages, system_prompt),
        }
        if tools:
            kwargs["tools"] = tools
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        resp = client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message

        tool_calls: list[CanonicalToolCall] = []
        if getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                fn = tc.function
                tool_calls.append(CanonicalToolCall(
                    id=tc.id,
                    name=fn.name,
                    arguments_json=fn.arguments or "{}",
                ))

        return CanonicalChatResponse(
            text=msg.content or "",
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "",
            raw=None,
        )

    def test_ping(self) -> dict[str, Any]:
        """admin 测试连接：发短 prompt 验证 api_key/base_url 可达。"""
        import time
        start = time.monotonic()
        try:
            resp = self.call(
                [{"role": "user", "content": "ping"}],
                max_tokens=8,
            )
            latency = int((time.monotonic() - start) * 1000)
            return {
                "ok": True,
                "latency_ms": latency,
                "sample": (resp.text or "")[:100],
                "model": self._model,
            }
        except Exception as exc:  # noqa: BLE001
            latency = int((time.monotonic() - start) * 1000)
            logger.warning(
                "openai_compat test_ping failed model=%s base=%s: %s",
                self._model, self._base_url, exc,
            )
            return {
                "ok": False,
                "latency_ms": latency,
                "error": str(exc)[:500],
            }

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Optional[list[dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Iterator[CanonicalStreamEvent]:
        client = self._client()
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": self._build_messages(messages, system_prompt),
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        # OpenAI streaming 的 tool_calls 是分段返回的：
        #   chunk.choices[0].delta.tool_calls 是 list，每项有 index + 可选 id/name/arguments(delta)
        # 我们按 index 累积，结束时合并产出 tool_call_end
        active_tool_calls: dict[int, dict[str, str]] = {}

        try:
            stream = client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.exception("openai stream create failed")
            yield CanonicalStreamEvent(type="error", error_message=str(exc))
            return

        try:
            for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                # 文本增量
                content = getattr(delta, "content", None)
                if content:
                    yield CanonicalStreamEvent(type="text", text=content)

                # tool call 增量
                if getattr(delta, "tool_calls", None):
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in active_tool_calls:
                            active_tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                            tc_id = getattr(tc_delta, "id", "") or ""
                            tc_name = ""
                            if getattr(tc_delta, "function", None):
                                tc_name = getattr(tc_delta.function, "name", "") or ""
                            active_tool_calls[idx]["id"] = tc_id
                            active_tool_calls[idx]["name"] = tc_name
                            if tc_id and tc_name:
                                yield CanonicalStreamEvent(
                                    type="tool_call_start",
                                    tool_call_id=tc_id,
                                    tool_call_name=tc_name,
                                )

                        # arguments 增量
                        if getattr(tc_delta, "function", None):
                            args_delta = getattr(tc_delta.function, "arguments", "") or ""
                            if args_delta:
                                active_tool_calls[idx]["arguments"] += args_delta
                                yield CanonicalStreamEvent(
                                    type="tool_call_args",
                                    tool_call_id=active_tool_calls[idx]["id"],
                                    arguments_delta=args_delta,
                                )
                        # 后到的 id/name（有些后端把 id 放在第一个 chunk 的 function 上）
                        new_id = getattr(tc_delta, "id", "") or ""
                        if new_id and not active_tool_calls[idx]["id"]:
                            active_tool_calls[idx]["id"] = new_id
                        if getattr(tc_delta, "function", None):
                            new_name = getattr(tc_delta.function, "name", "") or ""
                            if new_name and not active_tool_calls[idx]["name"]:
                                active_tool_calls[idx]["name"] = new_name

                # finish_reason 出现 = 这个 choice 结束
                fr = choice.finish_reason
                if fr:
                    # 所有 active tool_calls 结束
                    for idx in sorted(active_tool_calls.keys()):
                        tc = active_tool_calls[idx]
                        yield CanonicalStreamEvent(
                            type="tool_call_end",
                            tool_call_id=tc["id"],
                            tool_call_name=tc["name"],
                        )
                    active_tool_calls.clear()
                    yield CanonicalStreamEvent(type="done", finish_reason=fr)
                    return
        except Exception as exc:  # noqa: BLE001
            logger.exception("openai stream iteration failed")
            yield CanonicalStreamEvent(type="error", error_message=str(exc))
            return
