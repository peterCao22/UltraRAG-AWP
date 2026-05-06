"""
Gemini function calling 格式适配器。

职责：
1. OpenAI tools schema → Gemini functionDeclarations 格式转换
2. Gemini API 响应 → 标准 tool_call 列表解析
3. OpenAI messages → Gemini contents 格式转换
4. 工具结果 → Gemini functionResponse part 格式
5. GeminiLLMAdapter：封装请求体构造与 HTTP 调用
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def openai_tools_to_gemini(openai_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """将 OpenAI tools 格式转换为 Gemini functionDeclarations 格式。

    OpenAI:  {"type": "function", "function": {"name": ..., "parameters": ...}}
    Gemini:  {"name": ..., "description": ..., "parameters": ...}
    """
    result = []
    for tool in openai_tools:
        fn = tool.get("function") or tool
        decl: Dict[str, Any] = {"name": fn["name"]}
        if fn.get("description"):
            decl["description"] = fn["description"]
        if fn.get("parameters"):
            decl["parameters"] = fn["parameters"]
        result.append(decl)
    return result


def gemini_response_to_tool_calls(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 Gemini API 响应中提取 functionCall 列表。

    返回格式：[{"name": str, "args": dict}, ...]
    若无 functionCall（纯文本响应）返回空列表。
    """
    calls = []
    candidates = response.get("candidates") or []
    for candidate in candidates:
        parts = (candidate.get("content") or {}).get("parts") or []
        for part in parts:
            fc = part.get("functionCall")
            if fc:
                calls.append({
                    "name": fc.get("name", ""),
                    "args": fc.get("args") or {},
                })
    return calls


def gemini_response_extract_text(response: Dict[str, Any]) -> str:
    """从 Gemini API 响应中提取所有 text part 拼接后的字符串。"""
    parts_text = []
    candidates = response.get("candidates") or []
    for candidate in candidates:
        parts = (candidate.get("content") or {}).get("parts") or []
        for part in parts:
            t = part.get("text")
            if t:
                parts_text.append(t)
    return "".join(parts_text)


def messages_to_gemini_contents(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """将 OpenAI messages 格式转换为 Gemini contents 格式。

    - "user"      → role: "user"
    - "assistant" → role: "model"
    - "system"    → 跳过（system prompt 通过 systemInstruction 传递）
    - "tool"      → role: "user"，包含 functionResponse part
    """
    contents = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "system":
            continue
        gemini_role = "model" if role == "assistant" else "user"

        if role == "tool":
            # 工具结果消息
            part = {
                "functionResponse": {
                    "name": msg.get("name", ""),
                    "response": {"content": msg.get("content", "")},
                }
            }
            contents.append({"role": "user", "parts": [part]})
            continue

        content = msg.get("content")
        if isinstance(content, str):
            contents.append({"role": gemini_role, "parts": [{"text": content}]})
        elif isinstance(content, list):
            # OpenAI multipart content（文本+图片等）
            parts = []
            for block in content:
                if block.get("type") == "text":
                    parts.append({"text": block.get("text", "")})
            if parts:
                contents.append({"role": gemini_role, "parts": parts})
    return contents


def tool_result_to_gemini_part(tool_name: str, result: Any) -> Dict[str, Any]:
    """将工具执行结果包装为 Gemini functionResponse part。"""
    if isinstance(result, (dict, list)):
        response_content = result
    else:
        response_content = {"output": str(result)}
    return {
        "functionResponse": {
            "name": tool_name,
            "response": {"content": response_content},
        }
    }


class GeminiLLMAdapter:
    """Gemini REST API 适配器，封装 function calling 请求的构造与响应解析。"""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash",
        timeout: int = 120,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    def build_request_body(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
        generation_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """构造 Gemini generateContent 请求体。"""
        body: Dict[str, Any] = {
            "contents": messages_to_gemini_contents(messages),
        }
        if system_prompt:
            body["systemInstruction"] = {
                "parts": [{"text": system_prompt}]
            }
        if tools:
            body["tools"] = [{"functionDeclarations": tools}]
        if generation_config:
            body["generationConfig"] = generation_config
        return body

    def call(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
        generation_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """调用 Gemini generateContent API，返回原始响应 dict。"""
        body = self.build_request_body(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            generation_config=generation_config,
        )
        url = f"{GEMINI_API_BASE}/{self._model}:generateContent?key={self._api_key}"
        resp = requests.post(url, json=body, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()
