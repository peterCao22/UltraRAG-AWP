"""
AgentRunner: 真正的 ReAct（Reason + Act）引擎。

阶段 B 实现，与 RagRunner（阶段 A 单轮 RAG）并存。
chat.py 在 agent_mode=agent 时路由到此类。

架构：
  _build_initial_messages() → messages 列表
  _llm_call()               → 调用 Gemini，返回 {text, tool_calls}
  chat_stream()             → ReAct 主循环，yield SSE 事件 dict
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).parent.parent.parent / "prompt"

_TOOL_HINT_MAP: Dict[str, str] = {
    "knowledge_search": "搜索知识库",
    "keyword_search": "文本关键词搜索",
    "list_knowledge_chunks": "阅读文档：《{doc_id}》",
    "final_answer": "提交最终答案",
}

# tool_result.details 在 SSE 流出之前的最大字符数；前端折叠展开可读，落库前
# chat.py 还会再做一次保护性截断到 2000 字符
_TOOL_DETAILS_MAX_CHARS = 1500


def _format_tool_details(result: Any) -> str:
    """把工具原始返回值序列化成可在前端展开查看的字符串。

    截断策略：超过 _TOOL_DETAILS_MAX_CHARS 时硬截断并追加省略提示，避免单个
    SSE 帧过大或前端 details 折叠后渲染卡顿。
    """
    if isinstance(result, dict) and "error" in result:
        text = f"错误：{result['error']}"
    elif isinstance(result, str):
        text = result
    else:
        try:
            text = json.dumps(result, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            text = str(result)
    if len(text) > _TOOL_DETAILS_MAX_CHARS:
        text = text[:_TOOL_DETAILS_MAX_CHARS] + "\n…（已截断）"
    return text


class AgentRunner:
    """ReAct 推理引擎，驱动 Gemini function calling 循环。

    参数：
        kb_id: 知识库 ID，用于加载 FAISS 索引与语料。
        max_iterations: 最大 ReAct 轮次，超出后强制以当前上下文生成答案。
        enabled_tools: 允许的工具名列表；None 表示全部启用。
    """

    def __init__(
        self,
        kb_id: str,
        max_iterations: int = 12,
        enabled_tools: Optional[List[str]] = None,
    ) -> None:
        self.kb_id = kb_id
        self.max_iterations = max_iterations
        self.enabled_tools = enabled_tools
        self._rows: List[Dict[str, Any]] = []
        self._index: Any = None
        self._kb_name: str = kb_id
        self._registry: Any = None
        self._adapter: Any = None
        self._gemini_tools: Any = None

    def _ensure_attrs(self) -> None:
        """兼容 __new__ 构造（测试用）：确保核心属性存在。"""
        for attr, default in [
            ("_registry", None),
            ("_adapter", None),
            ("_gemini_tools", None),
            ("_rows", []),
            ("_index", None),
            ("_kb_name", self.kb_id if hasattr(self, "kb_id") else ""),
            ("max_iterations", 6),
            ("enabled_tools", None),
        ]:
            if not hasattr(self, attr):
                object.__setattr__(self, attr, default)

    # ─── 初始化 ────────────────────────────────────────────

    def init(self, rows: List[Dict[str, Any]], index: Any, kb_name: str = "") -> None:
        """注入 FAISS 索引与语料（由 chat.py 的 _get_agent_runner 调用）。"""
        from custom_app.services.tools.registry import ToolRegistry
        from custom_app.services.tools.knowledge_search import KnowledgeSearchTool
        from custom_app.services.tools.keyword_search import KeywordSearchTool
        from custom_app.services.tools.list_chunks import ListChunksTool
        from custom_app.services.tools.final_answer import FinalAnswerTool
        from custom_app.services.tools.query_knowledge_graph import QueryKnowledgeGraphTool

        self._rows = rows
        self._index = index
        self._kb_name = kb_name or self.kb_id

        self._registry = ToolRegistry()
        self._registry.register(KnowledgeSearchTool(rows=rows, index=index))
        self._registry.register(KeywordSearchTool(rows=rows))
        self._registry.register(ListChunksTool(rows=rows))
        self._registry.register(FinalAnswerTool())
        self._registry.register(QueryKnowledgeGraphTool(kb_id=self.kb_id))

        api_key = (
            os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("ULTRARAG_GEMINI_API_KEY")
            or ""
        )
        model = os.environ.get("ULTRARAG_GEMINI_MODEL", "gemini-2.0-flash")

        from custom_app.services.llm_adapter import GeminiLLMAdapter, openai_tools_to_gemini
        self._adapter = GeminiLLMAdapter(api_key=api_key, model=model)
        self._gemini_tools = openai_tools_to_gemini(
            self._registry.get_schemas(self.enabled_tools)
        )

    # ─── System Prompt ─────────────────────────────────────

    def _build_system_prompt(self, kb_name: str = "") -> str:
        try:
            env = Environment(loader=FileSystemLoader(str(_PROMPT_DIR)))
            tmpl = env.get_template("agv_agent_system.jinja")
            return tmpl.render(
                kb_name=kb_name or self._kb_name or self.kb_id,
                current_time=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                language="zh-CN",
                web_search_status="未启用",
            )
        except Exception:
            return (
                f"你是专业的工业设备知识库助手。知识库：{kb_name or self.kb_id}。"
                "必须调用 final_answer 工具提交最终答案。"
            )

    # ─── Messages 构建 ─────────────────────────────────────

    _HISTORY_LIMIT = 6  # 最多注入最近 N 条历史消息

    def _build_initial_messages(
        self,
        question: str,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """构造首轮 messages。

        history 为已落库的 user/assistant 轮次（最旧→最新），最多取最后
        _HISTORY_LIMIT 条注入到当前 user 问题之前。system prompt 通过
        systemInstruction 单独传给 Gemini，不放在 messages 内。
        """
        msgs: List[Dict[str, Any]] = []
        if history:
            for turn in history[-self._HISTORY_LIMIT:]:
                role = turn.get("role", "")
                content = str(turn.get("content", ""))
                if role in ("user", "assistant") and content:
                    msgs.append({"role": role, "content": content})
        msgs.append({"role": "user", "content": question})
        return msgs

    # ─── 工具提示 ──────────────────────────────────────────

    def _format_tool_hint(self, tool_name: str, args: Dict[str, Any]) -> str:
        if tool_name == "knowledge_search":
            q = str(args.get("query") or "")[:40]
            return f'搜索知识库："{q}"' if q else "搜索知识库"
        if tool_name == "keyword_search":
            kw = str(args.get("keywords") or "")[:40]
            return f'文本关键词搜索："{kw}"' if kw else "文本关键词搜索"
        if tool_name == "list_knowledge_chunks":
            doc = str(args.get("doc_id") or "")[:60]
            return f"阅读文档：《{doc}》" if doc else "阅读文档完整内容"
        if tool_name == "final_answer":
            return "提交最终答案"
        if tool_name == "query_knowledge_graph":
            ents = ", ".join(str(e) for e in args.get("entities", [])[:3])
            return f"知识图谱查询：[{ents}]" if ents else "知识图谱查询"
        return f"执行操作：{tool_name}"

    # ─── LLM 调用 ──────────────────────────────────────────

    def _llm_call(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: str,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """调用 Gemini API，返回 {text: str, tool_calls: list}。"""
        from custom_app.services.llm_adapter import (
            gemini_response_to_tool_calls,
            gemini_response_extract_text,
            messages_to_gemini_contents,
        )

        body = self._adapter.build_request_body(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
        )
        response = self._adapter.call(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
        )
        return {
            "text": gemini_response_extract_text(response),
            "tool_calls": gemini_response_to_tool_calls(response),
        }

    # ─── 工具执行 ──────────────────────────────────────────

    def _execute_tool(self, tool_name: str, args: Dict[str, Any]) -> Any:
        if self._registry is None:
            return {"error": "registry not initialized"}
        tool = self._registry.get(tool_name)
        if tool is None:
            return {"error": f"unknown tool: {tool_name}"}
        try:
            return tool.run(**args)
        except Exception as exc:
            logger.warning("Tool %s failed: %s", tool_name, exc)
            return {"error": str(exc)}

    # ─── messages 长度保护 ─────────────────────────────────

    @staticmethod
    def _trim_messages_if_needed(
        messages: List[Dict[str, Any]],
        max_chars: int = 40_000,
    ) -> List[Dict[str, Any]]:
        """精化截断：只截断工具结果消息（[工具结果 ...] 前缀），保护 user 原始问题。

        判定为工具结果的条件：role 为 user 且内容以 "[工具结果 " 开头。
        其他消息（user 原始问题、assistant 思考）不做截断。
        """
        trimmed = []
        for msg in messages:
            content = msg.get("content")
            if (
                isinstance(content, str)
                and len(content) > 2000
                and msg.get("role") == "user"
                and content.startswith("[工具结果 ")
            ):
                msg = {**msg, "content": content[:2000] + "\n…（已截断）"}
            trimmed.append(msg)
        return trimmed

    # ─── 耗尽轮次时强制合成答案 ────────────────────────────

    _SYNTHESIS_PROMPT = (
        "Based on the above tool call results, generate a complete answer for the user's question.\n"
        "User question: {question}\n"
        "Requirements:\n"
        "1. Answer based on the actually retrieved content\n"
        "2. Clearly cite information sources\n"
        "3. Organize the answer in a structured format\n"
        "4. If information is insufficient, honestly state so\n"
        "5. Respond in the same language as the user's question"
    )

    def _synthesize_final_answer(
        self,
        question: str,
        messages: List[Dict[str, Any]],
        system_prompt: str,
    ) -> str:
        """耗尽轮次时，重新调用 LLM 综合所有工具结果生成答案。

        参考 WeKnora 的 streamFinalAnswerToEventBus 实现：
        重建 messages = system_prompt + 原始问题 + 所有工具结果。
        """
        # 提取所有工具结果
        tool_results = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str) and content.startswith("[工具结果 "):
                tool_results.append(content)

        if not tool_results:
            return ""

        # 构建合成请求
        synthesis_messages = [
            {"role": "user", "content": question},
        ]
        for tr in tool_results:
            # 截断过长结果
            tr_short = tr[:2000] if len(tr) > 2000 else tr
            synthesis_messages.append({"role": "user", "content": tr_short})

        synthesis_messages.append({
            "role": "user",
            "content": self._SYNTHESIS_PROMPT.format(question=question),
        })

        try:
            result = self._llm_call(
                messages=synthesis_messages,
                system_prompt=system_prompt,
                tools=None,  # 不带工具，纯文本输出
            )
            return result.get("text", "").strip()
        except Exception as e:
            logger.error("Synthesis failed: %s", e)
            return ""

    # ─── 空响应重试 ────────────────────────────────────────

    _EMPTY_RETRY_NUDGE = (
        "Please provide your answer by calling the final_answer tool. "
        "You must call final_answer to submit your complete answer."
    )

    def _retry_empty_response(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: str,
        tools: Optional[List[Dict[str, Any]]],
        max_retries: int = 2,
    ) -> Optional[Dict[str, Any]]:
        """LLM 返回空内容且有工具结果时，重试并追加提示。

        参考 WeKnora 的 maxEmptyResponseRetries。
        """
        for attempt in range(max_retries):
            messages.append({"role": "user", "content": self._EMPTY_RETRY_NUDGE})
            result = self._llm_call(
                messages=messages,
                system_prompt=system_prompt,
                tools=tools,
            )
            text = result.get("text", "").strip()
            tcs = result.get("tool_calls") or []
            if text or any(tc.get("name") == "final_answer" for tc in tcs):
                return result
            logger.warning("Empty retry attempt %d failed", attempt + 1)
        return None

    # ─── ReAct 主循环 ──────────────────────────────────────

    def chat_stream(
        self,
        question: str,
        *,
        top_k: Optional[int] = None,
        profile: bool = False,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> Iterator[Dict[str, Any]]:
        """ReAct 主循环，yield SSE 事件 dict。

        事件类型：thought / tool_call / tool_result / chunk / done / error

        参数：
            history: 已落库的历史 user/assistant 消息列表（最旧→最新），
                     注入到当前问题之前，最多取最后 _HISTORY_LIMIT 条。
        """
        self._ensure_attrs()
        system_prompt = self._build_system_prompt()
        messages = self._build_initial_messages(question, history=history or [])
        if history:
            logger.info(
                "AgentRunner.chat_stream: history_turns=%d messages_built=%d",
                len(history), len(messages),
            )

        tools = None
        if self._registry is not None:
            from custom_app.services.llm_adapter import openai_tools_to_gemini
            tools = openai_tools_to_gemini(
                self._registry.get_schemas(self.enabled_tools)
            )

        final_answer_text = ""
        iteration = 0
        # 跨整个 ReAct 循环的已执行工具调用集合，用于去重
        executed_calls: set = set()

        try:
            while iteration < self.max_iterations:
                iteration += 1
                messages = self._trim_messages_if_needed(messages)

                # ── THINK ──────────────────────────────────
                llm_result = self._llm_call(
                    messages=messages,
                    system_prompt=system_prompt,
                    tools=tools,
                )

                thought_text = llm_result.get("text", "").strip()
                if thought_text:
                    yield {"type": "thought", "content": thought_text}

                tool_calls = llm_result.get("tool_calls") or []

                # 无工具调用 → LLM 直接给出文本答案（fallback）
                if not tool_calls:
                    if thought_text:
                        final_answer_text = thought_text
                        break
                    # 空响应重试（参考 WeKnora maxEmptyResponseRetries）
                    empty_result = self._retry_empty_response(
                        messages, system_prompt, tools,
                    )
                    if empty_result:
                        thought_text = empty_result.get("text", "").strip()
                        tool_calls = empty_result.get("tool_calls") or []
                        if thought_text:
                            yield {"type": "thought", "content": thought_text}
                        if not tool_calls:
                            final_answer_text = thought_text
                            break
                        # 有 tool_calls 继续执行
                    else:
                        # 重试也失败，继续下一轮
                        logger.warning("Empty response retry exhausted, continuing loop")
                        continue

                # ── ACT ────────────────────────────────────
                stop_loop = False
                for tc in tool_calls:
                    tool_name = tc.get("name", "")
                    args = tc.get("args") or {}

                    # ── 去重检查（final_answer 不去重）──────
                    if tool_name != "final_answer":
                        try:
                            call_key = (tool_name, json.dumps(args, sort_keys=True, ensure_ascii=False))
                        except (TypeError, ValueError):
                            call_key = (tool_name, str(args))
                        if call_key in executed_calls:
                            logger.debug("Dedup skip: %s %s", tool_name, args)
                            continue
                        executed_calls.add(call_key)

                    hint = self._format_tool_hint(tool_name, args)

                    yield {
                        "type": "tool_call",
                        "tool_name": tool_name,
                        "hint": hint,
                    }

                    # final_answer 直接从 args 取，不走工具执行路径
                    if tool_name == "final_answer":
                        final_answer_text = str(args.get("answer") or "")
                        stop_loop = True
                        yield {
                            "type": "tool_result",
                            "tool_name": tool_name,
                            "summary": "已生成最终答案",
                            "duration_ms": 1,
                        }
                        break

                    t0 = time.perf_counter()
                    result = self._execute_tool(tool_name, args)
                    duration_ms = max(int((time.perf_counter() - t0) * 1000), 1)

                    if tool_name == "final_answer" and isinstance(result, dict):
                        final_answer_text = result.get("answer", "")
                        stop_loop = True
                        yield {
                            "type": "tool_result",
                            "tool_name": tool_name,
                            "summary": "已生成最终答案",
                            "duration_ms": duration_ms,
                        }
                        break

                    # 汇总工具结果摘要
                    if isinstance(result, list):
                        summary = f"找到 {len(result)} 个结果"
                    elif isinstance(result, dict) and "error" in result:
                        summary = f"失败：{result['error']}"
                    else:
                        summary = "完成"

                    yield {
                        "type": "tool_result",
                        "tool_name": tool_name,
                        "summary": summary,
                        "duration_ms": duration_ms,
                        "details": _format_tool_details(result),
                    }

                    # ── OBSERVE：将结果追加到 messages ──────
                    result_str = (
                        json.dumps(result, ensure_ascii=False)
                        if not isinstance(result, str)
                        else result
                    )
                    messages.append({"role": "assistant", "content": thought_text or ""})
                    messages.append({
                        "role": "user",
                        "content": f"[工具结果 {tool_name}]\n{result_str[:3000]}",
                    })

                if stop_loop:
                    break

            else:
                # 超出 max_iterations，强制 LLM 合成答案（参考 WeKnora）
                if not final_answer_text:
                    logger.info(
                        "AgentRunner: max_iterations=%d reached, synthesizing answer",
                        self.max_iterations,
                    )
                    yield {"type": "thought", "content": "已达到最大推理轮次，正在综合已检索内容生成答案..."}
                    synthesized = self._synthesize_final_answer(
                        question, messages, system_prompt,
                    )
                    if synthesized:
                        final_answer_text = synthesized
                    else:
                        final_answer_text = "（已达到最大推理轮次，以上为当前检索到的内容。）"

        except Exception as exc:
            logger.exception("AgentRunner error: %s", exc)
            yield {"type": "error", "content": str(exc)}
            yield {"type": "done", "answer": ""}
            return

        # 流式输出最终答案
        if final_answer_text:
            yield {"type": "chunk", "content": final_answer_text}

        yield {
            "type": "done",
            "answer": final_answer_text,
            "meta": {
                "effective_agent_mode": "agent",
                "iterations": iteration,
            },
        }
