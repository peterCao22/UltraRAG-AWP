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
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiServiceUnavailable(RuntimeError):
    """Gemini REST API 在多次重试后仍不可达；前端可直接展示给用户。"""

# REST 鉴权：官方推荐在请求头中传递密钥（与文档 curl 示例一致），避免 URL 含 `?key=`。
# 参见 https://ai.google.dev/gemini-api/docs/api-key?hl=zh-cn 中 REST 小节（x-goog-api-key）。


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

    返回格式：[{"name": str, "args": dict, "thoughtSignature": str|...}, ...]
    thoughtSignature 位于响应 part 顶层（与 functionCall 并列）；Gemini 3
    下一轮请求必须原样带回，见 https://ai.google.dev/gemini-api/docs/thought-signatures

    若无 functionCall（纯文本响应）返回空列表。
    """
    calls = []
    candidates = response.get("candidates") or []
    for candidate in candidates:
        parts = (candidate.get("content") or {}).get("parts") or []
        for part in parts:
            fc = part.get("functionCall")
            if not fc:
                continue
            item: Dict[str, Any] = {
                "name": fc.get("name", ""),
                "args": fc.get("args") or {},
            }
            if "thoughtSignature" in part:
                item["thoughtSignature"] = part["thoughtSignature"]
            elif "thought_signature" in part:
                item["thoughtSignature"] = part["thought_signature"]
            calls.append(item)
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


def _merge_consecutive_gemini_contents(contents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """合并相邻同 role 的 content，满足 Gemini 对 turns 的约束并避免空片段。

    场景：跳过「空 assistant」后可能出现连续两条 user，需合并为一条 user，
    否则部分模型会返回 400。
    """
    if not contents:
        return contents
    out: List[Dict[str, Any]] = []
    for cur in contents:
        if not out:
            out.append({"role": cur["role"], "parts": list(cur.get("parts") or [])})
            continue
        prev = out[-1]
        if cur.get("role") == prev.get("role"):
            prev_parts = prev.get("parts") or []
            cur_parts = cur.get("parts") or []
            prev["parts"] = prev_parts + cur_parts
        else:
            out.append({"role": cur["role"], "parts": list(cur.get("parts") or [])})
    return out


def _model_needs_function_call_thought_signature(model_id: Optional[str]) -> bool:
    """Gemini 3 系列在 function calling 中强制校验 thoughtSignature（REST 须手动回传）。"""
    if not model_id:
        return False
    mid = model_id.lower()
    return "gemini-3" in mid


def messages_to_gemini_contents(
    messages: List[Dict[str, Any]],
    model_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """将 OpenAI messages 格式转换为 Gemini contents 格式。

    支持的 role 与 part 映射（关键：必须保留 assistant 的 functionCall 与
    tool 的 functionResponse 配对，否则 Gemini 看不到自己刚做过哪些调用，
    会出现"反复调用同一工具"的鬼打墙现象）：

    - "user"      → role: "user"          + text part
    - "assistant" → role: "model"
        · 若有 content 文本 → text part
        · 若有 tool_calls   → 每个生成一个 functionCall part（Gemini 多
          tool_calls 必须放进同一个 model 消息的 parts 数组中）
    - "system"    → 跳过（通过 systemInstruction 单独传递）
    - "tool"      → role: "user"          + functionResponse part
        · content 必须是已序列化字符串或 dict；这里包成
          response.content，与 Gemini 文档示例一致

    OpenAI 标准 messages 中 tool_calls 的形态为：
        {"role":"assistant", "content":"...", "tool_calls":[
            {"id":"...", "type":"function",
             "function":{"name":"X", "arguments":"<JSON 字符串>"}},
        ]}
    为兼容 agent_runner 内部更精简的写法，这里同时接受：
        {"role":"assistant", "content":"...", "tool_calls":[
            {"name":"X", "args":{...}}
        ]}
    """
    contents: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "system":
            continue

        if role == "tool":
            raw = msg.get("content", "")
            fn_name = msg.get("name", "") or msg.get("tool_name", "")
            if not fn_name:
                logger.warning("Skipping tool message with empty function name")
                continue
            if isinstance(raw, (dict, list)):
                response_content: Any = raw
            else:
                response_content = {"output": str(raw) if raw is not None else ""}
            part = {
                "functionResponse": {
                    "name": fn_name,
                    "response": {"content": response_content},
                }
            }
            contents.append({"role": "user", "parts": [part]})
            continue

        if role == "assistant":
            parts: List[Dict[str, Any]] = []
            content = msg.get("content")
            if isinstance(content, str) and content:
                parts.append({"text": content})
            elif isinstance(content, list):
                for block in content:
                    if block.get("type") == "text" and block.get("text"):
                        parts.append({"text": block["text"]})

            tool_calls_list = msg.get("tool_calls") or []
            for idx, tc in enumerate(tool_calls_list):
                fc_name = ""
                fc_args: Any = {}
                if "function" in tc and isinstance(tc["function"], dict):
                    fc_name = tc["function"].get("name", "") or ""
                    raw_args = tc["function"].get("arguments", "")
                    if isinstance(raw_args, str) and raw_args:
                        try:
                            fc_args = json.loads(raw_args)
                        except (TypeError, ValueError):
                            fc_args = {"_raw": raw_args}
                    elif isinstance(raw_args, dict):
                        fc_args = raw_args
                else:
                    fc_name = tc.get("name", "") or ""
                    fc_args = tc.get("args") or {}
                if not fc_name:
                    continue
                part_obj: Dict[str, Any] = {"functionCall": {"name": fc_name, "args": fc_args}}
                # Gemini 3：每个「当前轮」首条 functionCall 必须带 thoughtSignature；
                # 并行调用时仅第一条有签名，后续条不要伪造（见 thought-signatures 文档）。
                if "thoughtSignature" in tc:
                    part_obj["thoughtSignature"] = tc["thoughtSignature"]
                elif "thought_signature" in tc:
                    part_obj["thoughtSignature"] = tc["thought_signature"]
                elif _model_needs_function_call_thought_signature(model_id) and idx == 0:
                    part_obj["thoughtSignature"] = ""
                parts.append(part_obj)

            if not parts:
                # 空 assistant 且无 tool_calls：旧代码用 {"text": ""} 占位，
                # 部分 Gemini 模型会直接 400。应整条跳过，再由合并逻辑处理连续 user。
                continue
            contents.append({"role": "model", "parts": parts})
            continue

        # user 及其他默认走 user
        gemini_role = "user"
        content = msg.get("content")
        if isinstance(content, str):
            if not content:
                continue
            contents.append({"role": gemini_role, "parts": [{"text": content}]})
        elif isinstance(content, list):
            parts2: List[Dict[str, Any]] = []
            for block in content:
                if block.get("type") == "text" and block.get("text"):
                    parts2.append({"text": block["text"]})
            if parts2:
                contents.append({"role": gemini_role, "parts": parts2})
    return _merge_consecutive_gemini_contents(contents)


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
    """Gemini REST API 适配器，封装 function calling 请求的构造与响应解析。

    ``timeout`` 支持两种写法：
    - ``int`` / ``float``：兼容老调用，等价于 ``(10, timeout)`` 拆分（10s 连接 + N 秒读取）；
    - ``tuple[connect, read]``：直接透传给 requests，便于在更慢的网络环境调高 read 超时。
    """

    _DEFAULT_CONNECT_TIMEOUT = 10.0
    _DEFAULT_READ_TIMEOUT = 90.0
    _RETRY_ATTEMPTS = 2  # 第一次失败后再试 1 次；总尝试 2 次
    _RETRY_BACKOFF_SEC = 1.5

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash",
        timeout: Optional[float | Tuple[float, float]] = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout: Tuple[float, float] = self._normalize_timeout(timeout)

    @classmethod
    def _normalize_timeout(
        cls, timeout: Optional[float | Tuple[float, float]]
    ) -> Tuple[float, float]:
        if timeout is None:
            return (cls._DEFAULT_CONNECT_TIMEOUT, cls._DEFAULT_READ_TIMEOUT)
        if isinstance(timeout, tuple) and len(timeout) == 2:
            return (float(timeout[0]), float(timeout[1]))
        return (cls._DEFAULT_CONNECT_TIMEOUT, float(timeout))

    def build_request_body(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
        generation_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """构造 Gemini generateContent 请求体。"""
        body: Dict[str, Any] = {
            "contents": messages_to_gemini_contents(messages, model_id=self._model),
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
        """调用 Gemini generateContent API，返回原始响应 dict。

        关键容错：
        - 显式 ``(connect, read)`` 双超时，避免出现"卡在写 socket"无上限挂起；
        - 对网络层异常（Timeout / ConnectionError / write-aborted 等）做一次自动重试；
        - 重试仍失败时抛出 :class:`GeminiServiceUnavailable`，由上层把可读文案推到前端，
          而不是让前端等满 5 分钟才命中浏览器侧超时。
        """
        body = self.build_request_body(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            generation_config=generation_config,
        )
        url = f"{GEMINI_API_BASE}/{self._model}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self._api_key,
        }
        # 调试：记录请求体大小、消息数、工具数。出现 write timeout 时能立即
        # 判断是否 body 过大（>50KB 常是问题），还是网络层抖动。
        try:
            body_bytes = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            body_bytes = -1
        logger.info(
            "Gemini call model=%s body_bytes=%d contents=%d tools=%d",
            self._model,
            body_bytes,
            len(body.get("contents") or []),
            len((body.get("tools") or [{}])[0].get("functionDeclarations", []))
            if body.get("tools") else 0,
        )

        last_network_error: Optional[BaseException] = None
        for attempt in range(1, self._RETRY_ATTEMPTS + 1):
            try:
                resp = requests.post(
                    url, json=body, headers=headers, timeout=self._timeout
                )
            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
            ) as net_err:
                last_network_error = net_err
                logger.warning(
                    "Gemini network error model=%s attempt=%d/%d err=%s",
                    self._model,
                    attempt,
                    self._RETRY_ATTEMPTS,
                    net_err,
                )
                if attempt < self._RETRY_ATTEMPTS:
                    time.sleep(self._RETRY_BACKOFF_SEC * attempt)
                    continue
                raise GeminiServiceUnavailable(
                    "AI 服务暂时不可达（连接超时或被中止），请稍后重试。"
                    f"（详情: {type(net_err).__name__}）"
                ) from net_err

            if not resp.ok:
                err_body = (resp.text or "")[:2000]
                logger.error(
                    "Gemini generateContent failed model=%s status=%s body=%s",
                    self._model,
                    resp.status_code,
                    err_body,
                )
                # 5xx 视为暂时故障：让重试机会用完后才放弃；4xx 直接抛
                if 500 <= resp.status_code < 600 and attempt < self._RETRY_ATTEMPTS:
                    time.sleep(self._RETRY_BACKOFF_SEC * attempt)
                    continue
                resp.raise_for_status()
            return resp.json()

        # 理论不可达：循环正常退出会先 return；保留兜底以满足类型检查器。
        raise GeminiServiceUnavailable(
            "AI 服务暂时不可达，请稍后重试。"
        ) from last_network_error
