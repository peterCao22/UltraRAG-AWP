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
        chat_model: Optional[Dict[str, Any]] = None,  # Phase 7.1: chat_models 表的一行
        agent_config: Optional[Dict[str, Any]] = None,  # Phase 7.2.A: agent_configs 表的一行
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
        self._chat_model: Optional[Dict[str, Any]] = chat_model
        # Phase 7.2.A: agent_configs 行（agent_system_prompt 优先于 jinja 兜底）
        self._agent_config: Optional[Dict[str, Any]] = agent_config
        # KG 是否可用（init() 时查 kg 表行数），False 时不向 LLM 暴露 query_knowledge_graph，
        # 避免出现「开关开了但表是空的，LLM 反复尝试 KG 浪费轮次」的情况。
        self._kg_available: bool = True

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
            ("_source_builder", None),
            ("_id_to_row_idx", {}),
        ]:
            if not hasattr(self, attr):
                object.__setattr__(self, attr, default)

    # ─── 初始化 ────────────────────────────────────────────

    def init(
        self,
        rows: List[Dict[str, Any]],
        index: Any,
        kb_name: str = "",
        vector_store: Any = None,
        source_builder: Optional[Any] = None,
    ) -> None:
        """注入向量索引与语料（由 chat.py 的 _get_agent_runner 调用）。

        Args:
            rows:           chunks.jsonl 解析后的列表
            index:          FAISS index 对象或 Qdrant 模式下的 object() 占位符
            kb_name:        展示用名称
            vector_store:   VectorStore 实例（Qdrant/FAISS 统一接口），优先于 index
            source_builder: 可调用对象 ``build_sources(row_idx_list) -> List[dict]``，
                            用于在 final answer 后回构带图 ``sources``；通常传 RagRunner
                            的 ``_build_sources`` 即可（已实现 image base64 化）。
        """
        from custom_app.services.tools.registry import ToolRegistry
        from custom_app.services.tools.knowledge_search import KnowledgeSearchTool
        from custom_app.services.tools.keyword_search import KeywordSearchTool
        from custom_app.services.tools.list_chunks import ListChunksTool
        from custom_app.services.tools.final_answer import FinalAnswerTool
        from custom_app.services.tools.query_knowledge_graph import QueryKnowledgeGraphTool

        self._rows = rows
        self._index = index
        self._kb_name = kb_name or self.kb_id
        self._source_builder = source_builder
        # chunk_id → row_idx，用于事后从工具结果回查图片
        self._id_to_row_idx = {
            str(row.get("id", "")): i for i, row in enumerate(rows)
        }

        self._registry = ToolRegistry()
        self._registry.register(
            KnowledgeSearchTool(rows=rows, index=index, vector_store=vector_store)
        )
        self._registry.register(KeywordSearchTool(rows=rows))
        self._registry.register(ListChunksTool(rows=rows))
        self._registry.register(FinalAnswerTool())
        self._registry.register(QueryKnowledgeGraphTool(kb_id=self.kb_id))

        # Phase 7.1: 若调用方传入 chat_model（admin 配置），用 adapter 工厂；
        # 否则保留老路径 GeminiLLMAdapter（兼容无 chat_models 表的部署）
        if self._chat_model:
            from custom_app.services.chat_adapter_factory import resolve_chat_adapter
            self._adapter = resolve_chat_adapter(self._chat_model)
            self._adapter_canonical = True
            logger.info(
                "AgentRunner using canonical adapter: provider=%s model=%s",
                self._chat_model.get("provider"),
                self._chat_model.get("model_name"),
            )
        else:
            api_key = (
                os.environ.get("GOOGLE_API_KEY")
                or os.environ.get("ULTRARAG_GEMINI_API_KEY")
                or ""
            )
            model = os.environ.get("ULTRARAG_GEMINI_MODEL", "gemini-2.0-flash")
            from custom_app.services.llm_adapter import GeminiLLMAdapter
            self._adapter = GeminiLLMAdapter(api_key=api_key, model=model)
            self._adapter_canonical = False

        # 检测当前 KB 的 KG 是否真的有数据；空表则把 query_knowledge_graph 从生效集合
        # 中临时剔除，并在 system prompt 里告诉 LLM 不要尝试图谱查询。
        self._kg_available = self._detect_kg_available()
        if not self._kg_available:
            logger.info(
                "AgentRunner.init kb_id=%s: KG store empty, query_knowledge_graph disabled",
                self.kb_id,
            )

        # 老 Gemini 路径需要把 OpenAI 工具 schema 转 Gemini 格式；canonical 路径直接传 OpenAI 标准
        if not self._adapter_canonical:
            from custom_app.services.llm_adapter import openai_tools_to_gemini
            self._gemini_tools = openai_tools_to_gemini(
                self._registry.get_schemas(self._effective_enabled_tools())
            )
        else:
            # canonical 模式：直接拿 OpenAI 风格 schema，Adapter 内部转 provider 原生
            self._gemini_tools = self._registry.get_schemas(self._effective_enabled_tools())

    def _detect_kg_available(self) -> bool:
        """检查当前 KB 在 KG 后端里是否真的有实体/关系。

        失败时返回 True（保守策略，让 LLM 仍然可以尝试），仅在能确认空表时返回 False。
        """
        try:
            from custom_app.services.kg_search import get_graph_stats
            stats = get_graph_stats(self.kb_id) or {}
            entity_count = int(stats.get("entity_count") or 0)
            relation_count = int(stats.get("relation_count") or 0)
            return (entity_count + relation_count) > 0
        except Exception as exc:  # noqa: BLE001 - 任意后端异常都视为不可知
            logger.warning(
                "AgentRunner._detect_kg_available kb_id=%s failed: %s; assume available",
                self.kb_id, exc,
            )
            return True

    def _effective_enabled_tools(self) -> Optional[List[str]]:
        """在 self.enabled_tools 基础上根据运行时状态过滤掉不可用工具。

        当前规则：KG 空表时移除 query_knowledge_graph。
        若 enabled_tools 为 None（=全部启用），同样按规则过滤。
        """
        base = list(self.enabled_tools) if self.enabled_tools else None
        if self._kg_available:
            return base
        if base is None:
            # None 代表"全部已注册工具"，展开成名字列表再过滤
            if self._registry is not None:
                base = [t.name for t in self._registry.list_all()]
            else:
                return None
        return [t for t in base if t != "query_knowledge_graph"]

    # ─── System Prompt ─────────────────────────────────────

    def _build_system_prompt(self, kb_name: str = "") -> str:
        kg_status_note = (
            ""
            if self._kg_available
            else (
                "\n### 运行时状态：知识图谱（KG）不可用\n"
                "本知识库当前没有可用的实体关系图谱（实体数=0），"
                "**不要尝试调用 query_knowledge_graph 工具**；请直接走 "
                "knowledge_search → list_knowledge_chunks 链路完成回答。"
            )
        )
        # Phase 7.2.A：admin 配置的 agent_system_prompt 优先于 jinja 兜底
        custom = ""
        if self._agent_config:
            custom = (self._agent_config.get("agent_system_prompt") or "").strip()
        if custom:
            from custom_app.services.prompt_renderer import render_prompt

            base = render_prompt(
                custom,
                {
                    "kb_name": kb_name or self._kb_name or self.kb_id,
                    "kb_description": self._agent_config.get("kb_description") or "",
                    "language": "Chinese (Simplified)",
                    "current_time": datetime.now(timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "web_search_status": "未启用",
                },
            )
            return base + kg_status_note
        try:
            env = Environment(loader=FileSystemLoader(str(_PROMPT_DIR)))
            tmpl = env.get_template("agv_agent_system.jinja")
            base = tmpl.render(
                kb_name=kb_name or self._kb_name or self.kb_id,
                current_time=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                language="zh-CN",
                web_search_status="未启用",
            )
            return base + kg_status_note
        except Exception:
            return (
                f"你是专业的工业设备知识库助手。知识库：{kb_name or self.kb_id}。"
                "必须调用 final_answer 工具提交最终答案。" + kg_status_note
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

    @staticmethod
    def _format_executed_calls_note(executed_calls: set, max_items: int = 12) -> str:
        """根据本轮已执行的 (tool_name, args_json_str) 集合，构造"已执行清单"。

        注入到 system_prompt 末尾的目的（C 层）：即使 functionCall/Response 闭环
        已让 Gemini 看见自己做过什么，再加一条显式"已执行清单"作为"指令级提醒"，
        进一步降低重复请求概率。空集时返回空串以避免污染 prompt。

        - 截断：超过 max_items 时只列前 N 条 + 省略提示
        - 参数预览：每条调用参数最多取 80 字符，避免长 query 撑爆
        """
        if not executed_calls:
            return ""
        items = list(executed_calls)
        shown = items[:max_items]
        lines = ["", "### 本轮已执行的工具调用（请勿以相同参数重复调用）"]
        for tool_name, args_json in shown:
            preview = args_json
            if len(preview) > 80:
                preview = preview[:80] + "…"
            lines.append(f"- {tool_name}({preview})")
        if len(items) > max_items:
            lines.append(f"- …还有 {len(items) - max_items} 条已省略")
        lines.append(
            "若已有信息可作答，请立即调用 `final_answer`；"
            "若需补充检索，请使用**不同的参数或工具**。"
        )
        return "\n".join(lines)

    @staticmethod
    def _shrink_tool_payload(
        result: Any,
        max_items: int = 8,
        max_str_chars: int = 3000,
    ) -> Any:
        """把工具原始结果在写回 messages 之前做体积保护。

        - list：截前 max_items 条；超出时附加 _truncated 标记，便于 LLM 判断
        - dict：保持结构（KG 返回的 entity/relation 字段都很短），仅在最外层 dict
          包装时也加 _truncated 标记
        - 其他：转字符串后按 max_str_chars 截断

        注意不要"破坏结构本身"，否则 Gemini 端 functionResponse 会丢失字段语义。
        """
        if isinstance(result, list):
            if len(result) > max_items:
                trimmed = list(result[:max_items])
                trimmed.append({
                    "_truncated": True,
                    "_omitted": len(result) - max_items,
                    "_hint": f"已省略 {len(result) - max_items} 条结果，避免上下文过长",
                })
                return trimmed
            return result
        if isinstance(result, dict):
            try:
                serialized = json.dumps(result, ensure_ascii=False)
            except (TypeError, ValueError):
                serialized = str(result)
            if len(serialized) > max_str_chars:
                # 字典体积过大时，退化为带截断提示的 dict 包装，避免破坏结构
                return {
                    "_truncated": True,
                    "_preview": serialized[:max_str_chars] + "…",
                }
            return result
        text = str(result) if result is not None else ""
        if len(text) > max_str_chars:
            text = text[:max_str_chars] + "…（已截断）"
        return {"output": text}

    @staticmethod
    def _format_dedup_feedback(tool_name: str, args: Dict[str, Any]) -> str:
        """构造"重复调用被拦截"的反馈，回喂给 LLM。

        关键设计：
        1. 明确告诉 LLM 这次调用被拦截了，避免它以为是网络/权限问题继续重试；
        2. 提示参数已存在（用 args 摘要），让它知道是哪个调用；
        3. 强约束下一步只能 final_answer，避免它换个等价参数继续打转；
        4. 不暴露 executed_calls 完整集合，避免 prompt 膨胀。
        """
        try:
            args_brief = json.dumps(args, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            args_brief = str(args)
        if len(args_brief) > 200:
            args_brief = args_brief[:200] + "…"
        return (
            f"⚠️ 工具调用已被拦截：本轮检测到你正以相同参数重复调用 `{tool_name}`，"
            f"该调用在前面的轮次已经执行过（参数：{args_brief}）。\n"
            f"为节省时间，本次未重新执行。\n\n"
            f"👉 下一步必须做的事：\n"
            f"1. 不要再次调用 `{tool_name}`（无论参数微调与否）；\n"
            f"2. 基于已有工具结果（在前面的对话中可见），立即调用 `final_answer` 提交答案；\n"
            f"3. 如果你认为已有信息确实不足以回答，也请调用 `final_answer` 并在答案中说明"
            f"\"现有资料无法直接回答 X\"，不要再发起重复检索。"
        )

    @staticmethod
    def _tool_call_dict_for_history(tc: Dict[str, Any]) -> Dict[str, Any]:
        """构造写入 messages 的 tool_calls 项，保留 Gemini 3 的 thoughtSignature。"""
        d: Dict[str, Any] = {
            "name": tc.get("name", ""),
            "args": tc.get("args") or {},
        }
        if "thoughtSignature" in tc:
            d["thoughtSignature"] = tc["thoughtSignature"]
        elif "thought_signature" in tc:
            d["thoughtSignature"] = tc["thought_signature"]
        return d

    # ─── LLM 调用 ──────────────────────────────────────────

    def _llm_call(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: str,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """调用 LLM，返回 {text: str, tool_calls: list}。

        - canonical adapter（Phase 7.1）：直接返回 CanonicalChatResponse 转 dict
        - 老 Gemini adapter（向后兼容）：用 gemini_response_to_tool_calls 解析
        """
        if getattr(self, "_adapter_canonical", False):
            resp = self._adapter.call(
                messages,
                tools=tools,
                system_prompt=system_prompt,
            )
            # CanonicalToolCall → AgentRunner 内部使用的 dict 格式
            # 字段对齐老路径：{name, args}；额外保留 id 供 OpenAI/Anthropic 多轮 tool_call_id 关联
            tool_calls = []
            for tc in resp.tool_calls:
                try:
                    args = json.loads(tc.arguments_json or "{}")
                except Exception:
                    args = {}
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.name,
                    "args": args if isinstance(args, dict) else {},
                })
            return {"text": resp.text or "", "tool_calls": tool_calls}

        # 老路径：Gemini 原生 adapter
        from custom_app.services.llm_adapter import (
            gemini_response_to_tool_calls,
            gemini_response_extract_text,
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
        """精化截断：只截断工具结果消息，保护 user 原始问题。

        判定为工具结果的条件（任一即可，覆盖新旧两种协议）：
        - role == "tool"：标准 function calling 闭环写回（A 方案后的主路径）
        - role == "user" 且 content 以 "[工具结果 " 开头：旧字符串拼接路径
          （保留兼容，便于历史 messages 串联场景）
        其他消息（user 原始问题、assistant 思考）不做截断。
        """
        trimmed = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")

            if role == "tool":
                # tool.content 可能是 dict/list/str，统一序列化后判定长度
                if isinstance(content, str):
                    text = content
                else:
                    try:
                        text = json.dumps(content, ensure_ascii=False)
                    except (TypeError, ValueError):
                        text = str(content)
                if len(text) > 2000:
                    msg = {
                        **msg,
                        "content": {
                            "_truncated": True,
                            "_preview": text[:2000] + "…（已截断）",
                        },
                    }
                trimmed.append(msg)
                continue

            if (
                isinstance(content, str)
                and len(content) > 2000
                and role == "user"
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
    ) -> Dict[str, Any]:
        """耗尽轮次时，重新调用 LLM 综合所有工具结果生成答案。

        参考 WeKnora 的 streamFinalAnswerToEventBus 实现：
        重建 messages = system_prompt + 原始问题 + 所有工具结果。

        返回结构（统一为 dict，避免历史代码"返回空串=未知失败"的歧义）：
            {
              "text": str,            # 综合后的答案；失败时为 ""
              "tool_result_count": int,
              "error": Optional[str], # 失败原因（gemini_error / no_tool_result / empty_response）
              "error_detail": str,    # 给用户看的可读说明（已截断）
            }
        """
        # A 方案后主路径是 role=="tool" 的标准消息；旧字符串前缀路径仍兼容
        # （便于跨版本 messages 串联或历史会话回放）。
        tool_results: List[str] = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            if role == "tool":
                tool_name = msg.get("name", "") or msg.get("tool_name", "")
                if isinstance(content, str):
                    serialized = content
                else:
                    try:
                        serialized = json.dumps(content, ensure_ascii=False)
                    except (TypeError, ValueError):
                        serialized = str(content)
                tool_results.append(f"[工具结果 {tool_name}]\n{serialized}")
                continue
            if isinstance(content, str) and content.startswith("[工具结果 "):
                tool_results.append(content)

        if not tool_results:
            return {
                "text": "",
                "tool_result_count": 0,
                "error": "no_tool_result",
                "error_detail": "已耗尽推理轮次但未收到任何工具结果，可能是 LLM 始终未发起检索。",
            }

        synthesis_messages = [
            {"role": "user", "content": question},
        ]
        for tr in tool_results:
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
            text = (result.get("text") or "").strip()
            if not text:
                logger.warning("Synthesis returned empty text (tool_results=%d)", len(tool_results))
                return {
                    "text": "",
                    "tool_result_count": len(tool_results),
                    "error": "empty_response",
                    "error_detail": "综合调用返回空文本，可能是 LLM 拒答或被安全策略阻断。",
                }
            return {
                "text": text,
                "tool_result_count": len(tool_results),
                "error": None,
                "error_detail": "",
            }
        except Exception as e:
            logger.exception("Synthesis failed")
            return {
                "text": "",
                "tool_result_count": len(tool_results),
                "error": "gemini_error",
                "error_detail": str(e)[:300],
            }

    # ─── 工具证据摘要（合成失败兜底用）──────────────────────

    @staticmethod
    def _collect_tool_evidence_summary(
        messages: List[Dict[str, Any]],
        max_items: int = 5,
        max_chars_per_item: int = 200,
    ) -> str:
        """从 messages 里抽取最多 N 条工具结果，给用户一个"看得见的证据"。

        失败合成兜底使用。仅做粗粒度摘要：每条取前 max_chars_per_item 字符，
        足以让用户判断"是否有相关命中"，但又不会让答案变成超长 JSON 转储。
        """
        items: List[str] = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            text: Optional[str] = None
            if role == "tool":
                tool_name = msg.get("name", "") or msg.get("tool_name", "")
                if isinstance(content, str):
                    serialized = content
                else:
                    try:
                        serialized = json.dumps(content, ensure_ascii=False)
                    except (TypeError, ValueError):
                        serialized = str(content)
                text = f"[工具结果 {tool_name}] {serialized}"
            elif isinstance(content, str) and content.startswith("[工具结果 "):
                text = content
            if text is None:
                continue
            head = text[:max_chars_per_item].replace("\n", " ")
            if len(text) > max_chars_per_item:
                head += " …"
            items.append(f"- {head}")
            if len(items) >= max_items:
                break
        return "\n".join(items)

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
            # 注意：chat.py 每次请求会刷新 self.enabled_tools，因此这里要再算一次
            # 有效集合（剔除空 KG 时的 query_knowledge_graph）。
            schemas = self._registry.get_schemas(self._effective_enabled_tools())
            if getattr(self, "_adapter_canonical", False):
                # Phase 7.1 canonical 路径（OpenAI / Anthropic）：直接传 OpenAI 标准
                # 嵌套 schema；Anthropic adapter 内部再转 input_schema。
                tools = schemas
            else:
                # 老 Gemini 原生路径：需扁平化成 Gemini functionDeclarations
                from custom_app.services.llm_adapter import openai_tools_to_gemini
                tools = openai_tools_to_gemini(schemas)

        final_answer_text = ""
        iteration = 0
        # 跨整个 ReAct 循环的已执行工具调用集合，用于去重
        executed_calls: set = set()
        # Agent 在工具执行过程中接触到的 chunk_id（按命中顺序去重），
        # 用于回构带图 sources 事件 —— 即使 LLM 没在 markdown 里写 ![](URL)，
        # 前端仍能从 sources 块看到引用图片。
        cited_chunk_ids: List[str] = []
        seen_chunk_ids: set = set()

        try:
            while iteration < self.max_iterations:
                iteration += 1
                messages = self._trim_messages_if_needed(messages)

                # 动态把"已执行清单"注入到 system_prompt 末尾（C 层双保险）。
                # 不修改基础 prompt，只在调用前拼接，避免污染 self.* 状态。
                exec_note = self._format_executed_calls_note(executed_calls)
                effective_system_prompt = system_prompt + exec_note if exec_note else system_prompt

                # ── THINK ──────────────────────────────────
                llm_result = self._llm_call(
                    messages=messages,
                    system_prompt=effective_system_prompt,
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
                        messages, effective_system_prompt, tools,
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
                # 关键：写回 messages 必须使用标准 function calling 协议闭环：
                #   assistant.tool_calls + tool role
                # 这样 messages_to_gemini_contents 能把它们转成 model.functionCall +
                # user.functionResponse part，让 Gemini 在下一轮看到"自己刚做过的调用"，
                # 不会重复发起同样的 functionCall。
                stop_loop = False

                # 把本轮 LLM 输出的所有 tool_calls 一次性记录到 assistant 消息中
                # （Gemini 同一 model 消息可含多个 functionCall part）。
                assistant_msg: Dict[str, Any] = {
                    "role": "assistant",
                    "content": thought_text or "",
                    "tool_calls": [
                        AgentRunner._tool_call_dict_for_history(tc)
                        for tc in tool_calls
                    ],
                }
                messages.append(assistant_msg)

                for tc in tool_calls:
                    tool_name = tc.get("name", "")
                    args = tc.get("args") or {}

                    # ── 去重检查（final_answer 不去重，作为 D 层兜底）──────
                    # 协议闭环修好后，Gemini 几乎不应该再走到这里；一旦走到说明
                    # 模型层面失控，仍然把"该调用已被拦截"作为合成 tool 结果回喂。
                    if tool_name != "final_answer":
                        try:
                            call_key = (tool_name, json.dumps(args, sort_keys=True, ensure_ascii=False))
                        except (TypeError, ValueError):
                            call_key = (tool_name, str(args))
                        if call_key in executed_calls:
                            logger.info(
                                "Dedup hit (协议闭环后异常路径), injecting feedback: %s %s",
                                tool_name, args,
                            )
                            hint = self._format_tool_hint(tool_name, args)
                            yield {
                                "type": "tool_call",
                                "tool_name": tool_name,
                                "hint": hint + "（重复调用，已拦截）",
                            }
                            dedup_message = self._format_dedup_feedback(tool_name, args)
                            yield {
                                "type": "tool_result",
                                "tool_name": tool_name,
                                "summary": "重复调用已拦截，请直接调用 final_answer 提交答案",
                                "duration_ms": 1,
                                "details": dedup_message,
                            }
                            messages.append({
                                "role": "tool",
                                "name": tool_name,
                                "content": {"intercepted": True, "reason": dedup_message},
                            })
                            continue
                        executed_calls.add(call_key)

                    hint = self._format_tool_hint(tool_name, args)

                    yield {
                        "type": "tool_call",
                        "tool_name": tool_name,
                        "hint": hint,
                    }

                    # final_answer：直接从 args 取，不走 _execute_tool。但仍要写一条
                    # tool 结果消息保持协议闭环（即使这一轮就要 break）。
                    if tool_name == "final_answer":
                        final_answer_text = str(args.get("answer") or "")
                        stop_loop = True
                        yield {
                            "type": "tool_result",
                            "tool_name": tool_name,
                            "summary": "已生成最终答案",
                            "duration_ms": 1,
                        }
                        messages.append({
                            "role": "tool",
                            "name": tool_name,
                            "content": {"answer": final_answer_text, "stop": True},
                        })
                        break

                    t0 = time.perf_counter()
                    result = self._execute_tool(tool_name, args)
                    duration_ms = max(int((time.perf_counter() - t0) * 1000), 1)

                    # 收集本次工具结果里的 chunk_id，用于最终带图 sources。
                    # knowledge_search / keyword_search / list_knowledge_chunks 等都返回
                    # list[dict]，每个 dict 含 "id"=chunk_id。
                    if isinstance(result, list):
                        for item in result:
                            if not isinstance(item, dict):
                                continue
                            cid = str(item.get("id") or "")
                            if cid and cid not in seen_chunk_ids:
                                seen_chunk_ids.add(cid)
                                cited_chunk_ids.append(cid)

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

                    # ── OBSERVE：将结果按 function calling 协议写回 ──────
                    # Gemini 期望 functionResponse.response.content 为结构化对象，
                    # 这里直接放原始 result（list/dict），由 llm_adapter 统一打包。
                    # 大体积时调用 _shrink_tool_payload 做截断，避免 prompt 爆炸。
                    tool_content_payload = self._shrink_tool_payload(result)
                    messages.append({
                        "role": "tool",
                        "name": tool_name,
                        "content": tool_content_payload,
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
                    synth = self._synthesize_final_answer(
                        question, messages, system_prompt,
                    )
                    if synth.get("text"):
                        final_answer_text = synth["text"]
                    else:
                        # 把"为什么没出答案"明确写出来，避免用户看到一行通用文案
                        evidence = self._collect_tool_evidence_summary(messages)
                        reason = synth.get("error_detail") or "未知原因"
                        parts = [
                            "**很抱歉，本次未能生成最终答案。**",
                            f"- 已达到最大推理轮次：{self.max_iterations}",
                            f"- 综合阶段失败原因：{reason}",
                            f"- 已检索到的证据条目数：{synth.get('tool_result_count', 0)}",
                        ]
                        if evidence:
                            parts.append("\n**检索过程中累积的证据摘要：**\n" + evidence)
                        parts.append(
                            "\n排查建议：查看 `logs/app.log` 最近一次 `Synthesis failed` "
                            "或 `Synthesis returned empty text` 日志，确认 LLM 调用错误详情。"
                        )
                        final_answer_text = "\n".join(parts)

        except Exception as exc:
            from custom_app.services.llm_adapter import GeminiServiceUnavailable

            logger.exception("AgentRunner error: %s", exc)
            if isinstance(exc, GeminiServiceUnavailable):
                # 网络/上游临时故障：把可读文案推给前端，避免裸异常 repr。
                user_text = str(exc) or "AI 服务暂时不可达，请稍后重试。"
            else:
                user_text = f"智能推理出错：{exc}"
            yield {"type": "error", "content": user_text}
            yield {"type": "done", "answer": ""}
            return

        # 流式输出最终答案
        if final_answer_text:
            yield {"type": "chunk", "content": final_answer_text}

        # 用 RagRunner 的 _build_sources 把本轮接触到的 chunk_id 转成带图 sources。
        # 即使 LLM 没在答案 markdown 里写 ![](URL)，前端仍能从 sources 块看到引用图片。
        if cited_chunk_ids and self._source_builder is not None:
            row_indices = [
                self._id_to_row_idx[cid]
                for cid in cited_chunk_ids
                if cid in self._id_to_row_idx
            ]
            if row_indices:
                try:
                    sources = self._source_builder(row_indices)
                    if sources:
                        yield {"type": "sources", "sources": sources}
                except Exception:
                    logger.exception("agent sources build failed; skipping image attach")

        yield {
            "type": "done",
            "answer": final_answer_text,
            "meta": {
                "effective_agent_mode": "agent",
                "iterations": iteration,
            },
        }
