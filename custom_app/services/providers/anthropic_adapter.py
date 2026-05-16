"""Phase 7.1: Anthropic Claude 完整适配器。

依赖 anthropic SDK（pip install anthropic）。lazy import 让缺包时不影响其它路径。

实现要点：
    - canonical messages（OpenAI 风格 role）→ Anthropic 原生 messages
      * system 抽出来传 system 参数
      * role=tool 的消息转为 user.content_blocks 里的 tool_result
      * assistant.tool_calls 转为 assistant.content_blocks 里的 tool_use
    - canonical tools → Anthropic input_schema 格式
    - 响应 content blocks → canonical text + tool_calls
    - streaming 走 Anthropic 的 message_start / content_block_start / delta / stop 事件序列

Anthropic 新模型（Opus 4.x / Sonnet 4.x）已弃用 temperature；不传时 SDK 用模型默认。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterator, Optional

from custom_app.services.providers.llm_protocol import (
    CanonicalChatResponse,
    CanonicalStreamEvent,
    CanonicalToolCall,
)

logger = logging.getLogger(__name__)


class AnthropicDependencyMissing(RuntimeError):
    """anthropic SDK 未安装。"""


# Anthropic 必填 max_tokens；这是个保守默认值
_DEFAULT_MAX_TOKENS = 4096


class AnthropicAdapter:
    """Anthropic Claude 适配器：完整 call + stream + tool calling。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "",
        timeout: float = 300.0,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = (base_url or "").strip().rstrip("/")
        self._timeout = float(timeout)
        self._extra = extra or {}

    def model_name(self) -> str:
        return self._model

    def _client(self):
        try:
            from anthropic import Anthropic  # type: ignore
        except ImportError as e:
            raise AnthropicDependencyMissing(
                "anthropic SDK not installed; run `pip install anthropic`"
            ) from e
        kwargs: dict[str, Any] = {"api_key": self._api_key, "timeout": self._timeout}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        return Anthropic(**kwargs)

    # ─── canonical → Anthropic 转换 ──────────────────────────────────────

    @staticmethod
    def _convert_messages(
        messages: list[dict[str, Any]],
        system_prompt: Optional[str],
    ) -> tuple[list[dict[str, Any]], str]:
        """canonical messages → (anthropic_messages, system_text)。

        转换规则：
            - role=system 的消息合并进 system 参数（不放入 messages）
            - role=user / role=assistant 大致原样保留，但 assistant.tool_calls 要
              转成 content blocks 的 tool_use
            - role=tool 的消息转成 user.content_blocks 的 tool_result
            - 连续相同 role 不合并（Anthropic 允许）
        """
        sys_chunks: list[str] = []
        if system_prompt:
            sys_chunks.append(system_prompt)

        out: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content")
            if role == "system":
                if content:
                    if isinstance(content, str):
                        sys_chunks.append(content)
                    else:
                        sys_chunks.append(str(content))
                continue

            if role == "tool":
                # canonical: {role: "tool", tool_call_id?, name?, content}
                # AgentRunner 简化格式没 tool_call_id；用 name 推回最近 assistant
                # 的 tool_use id（前一条 assistant 消息里）
                tool_call_id = m.get("tool_call_id", "") or ""
                if not tool_call_id and m.get("name") and out:
                    # 往前找最近的 assistant，取 name 匹配的 tool_use id
                    for prev in reversed(out):
                        if prev.get("role") != "assistant":
                            continue
                        for blk in prev.get("content", []):
                            if (isinstance(blk, dict)
                                and blk.get("type") == "tool_use"
                                and blk.get("name") == m["name"]):
                                tool_call_id = blk.get("id", "")
                                break
                        if tool_call_id:
                            break
                if not isinstance(content, str):
                    try:
                        content_text = json.dumps(content, ensure_ascii=False)
                    except (TypeError, ValueError):
                        content_text = str(content)
                else:
                    content_text = content
                out.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_call_id or f"call_{m.get('name', 'unknown')}",
                        "content": content_text,
                    }],
                })
                continue

            if role == "assistant":
                tool_calls = m.get("tool_calls") or []
                blocks: list[dict[str, Any]] = []
                # 先文本（如果有）
                if isinstance(content, str) and content:
                    blocks.append({"type": "text", "text": content})
                for tc in tool_calls:
                    # 两种 dict 兼容：
                    #   1) OpenAI 标准 {id, type, function: {name, arguments(JSON str)}}
                    #   2) AgentRunner 简化 {id?, name, args(dict)}
                    if tc.get("function"):
                        fn = tc["function"]
                        name = fn.get("name", "")
                        try:
                            args = json.loads(fn.get("arguments") or "{}")
                        except (TypeError, ValueError):
                            args = {}
                    else:
                        name = tc.get("name", "")
                        args = tc.get("args") or {}
                    if not isinstance(args, dict):
                        args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", "") or f"call_{name}",
                        "name": name,
                        "input": args,
                    })
                if not blocks:
                    blocks = [{"type": "text", "text": ""}]
                out.append({"role": "assistant", "content": blocks})
                continue

            # user 或其它：直接保留
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
            elif isinstance(content, list):
                out.append({"role": "user", "content": content})
            else:
                out.append({"role": "user", "content": str(content or "")})

        # Anthropic 要求 messages 不能以 assistant 开头（必须 user 开头），且最后
        # 一条不能是 assistant（除非显式 prefill，但我们不用）；调用方保证 OK。
        return out, "\n\n".join(sys_chunks)

    @staticmethod
    def _convert_tools(tools: Optional[list[dict[str, Any]]]) -> Optional[list[dict[str, Any]]]:
        """OpenAI 风格 tools → Anthropic 风格。

        OpenAI:   {type: "function", function: {name, description, parameters}}
        Anthropic: {name, description, input_schema}
        """
        if not tools:
            return None
        out: list[dict[str, Any]] = []
        for t in tools:
            fn = t.get("function") or t  # 兼容已是 anthropic 风格的
            name = fn.get("name", "")
            desc = fn.get("description", "")
            params = fn.get("parameters") or fn.get("input_schema") or {"type": "object", "properties": {}}
            out.append({
                "name": name,
                "description": desc,
                "input_schema": params,
            })
        return out

    @staticmethod
    def _parse_response_content(resp) -> tuple[str, list[CanonicalToolCall]]:
        """Anthropic response.content (list of blocks) → (text, tool_calls)。"""
        text_parts: list[str] = []
        tool_calls: list[CanonicalToolCall] = []
        for block in getattr(resp, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", "") or "")
            elif btype == "tool_use":
                tool_calls.append(CanonicalToolCall(
                    id=getattr(block, "id", "") or "",
                    name=getattr(block, "name", "") or "",
                    arguments_json=json.dumps(
                        getattr(block, "input", {}) or {},
                        ensure_ascii=False,
                    ),
                ))
        return "".join(text_parts), tool_calls

    # ─── Protocol 方法 ──────────────────────────────────────────────────

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
        an_messages, sys_text = self._convert_messages(messages, system_prompt)
        an_tools = self._convert_tools(tools)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens or _DEFAULT_MAX_TOKENS,
            "messages": an_messages,
        }
        if sys_text:
            kwargs["system"] = sys_text
        if an_tools:
            kwargs["tools"] = an_tools
        # temperature 仅在调用方明确传值时才设（新模型已弃用，传了会报错）
        if temperature is not None:
            kwargs["temperature"] = temperature

        resp = client.messages.create(**kwargs)
        text, tool_calls = self._parse_response_content(resp)

        return CanonicalChatResponse(
            text=text,
            tool_calls=tool_calls,
            finish_reason=getattr(resp, "stop_reason", "") or "",
            raw=None,
        )

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
        an_messages, sys_text = self._convert_messages(messages, system_prompt)
        an_tools = self._convert_tools(tools)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens or _DEFAULT_MAX_TOKENS,
            "messages": an_messages,
        }
        if sys_text:
            kwargs["system"] = sys_text
        if an_tools:
            kwargs["tools"] = an_tools
        if temperature is not None:
            kwargs["temperature"] = temperature

        # Anthropic streaming：用 client.messages.stream() context manager；
        # 它产出的事件类型：message_start / content_block_start /
        # content_block_delta / content_block_stop / message_delta / message_stop
        # tool_use 块的参数也是按 input_json_delta 流式回传
        active_tool_calls: dict[int, dict[str, str]] = {}
        try:
            with client.messages.stream(**kwargs) as stream:
                for event in stream:
                    etype = getattr(event, "type", "")
                    if etype == "content_block_start":
                        block = getattr(event, "content_block", None)
                        idx = getattr(event, "index", 0)
                        btype = getattr(block, "type", "") if block else ""
                        if btype == "tool_use":
                            tc_id = getattr(block, "id", "") or ""
                            tc_name = getattr(block, "name", "") or ""
                            active_tool_calls[idx] = {"id": tc_id, "name": tc_name, "arguments": ""}
                            yield CanonicalStreamEvent(
                                type="tool_call_start",
                                tool_call_id=tc_id,
                                tool_call_name=tc_name,
                            )
                    elif etype == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        dtype = getattr(delta, "type", "") if delta else ""
                        idx = getattr(event, "index", 0)
                        if dtype == "text_delta":
                            text = getattr(delta, "text", "") or ""
                            if text:
                                yield CanonicalStreamEvent(type="text", text=text)
                        elif dtype == "input_json_delta":
                            partial = getattr(delta, "partial_json", "") or ""
                            if partial and idx in active_tool_calls:
                                active_tool_calls[idx]["arguments"] += partial
                                yield CanonicalStreamEvent(
                                    type="tool_call_args",
                                    tool_call_id=active_tool_calls[idx]["id"],
                                    arguments_delta=partial,
                                )
                    elif etype == "content_block_stop":
                        idx = getattr(event, "index", 0)
                        if idx in active_tool_calls:
                            tc = active_tool_calls.pop(idx)
                            yield CanonicalStreamEvent(
                                type="tool_call_end",
                                tool_call_id=tc["id"],
                                tool_call_name=tc["name"],
                            )
                    elif etype == "message_delta":
                        # 含 stop_reason 时记下来
                        delta = getattr(event, "delta", None)
                        sr = getattr(delta, "stop_reason", "") if delta else ""
                        if sr:
                            yield CanonicalStreamEvent(
                                type="done",
                                finish_reason=sr,
                            )
                            return
                    elif etype == "message_stop":
                        # 兜底
                        yield CanonicalStreamEvent(type="done", finish_reason="stop")
                        return
        except Exception as exc:  # noqa: BLE001
            logger.exception("anthropic stream failed")
            yield CanonicalStreamEvent(type="error", error_message=str(exc))
            return

    # ─── 兼容 Phase 7 旧版 test_ping（admin 测试连接保留） ──────────

    def test_ping(self) -> dict[str, Any]:
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
                "anthropic test_ping failed model=%s base=%s: %s",
                self._model, self._base_url, exc,
            )
            return {
                "ok": False,
                "latency_ms": latency,
                "error": str(exc)[:500],
            }
