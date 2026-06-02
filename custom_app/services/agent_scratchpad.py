"""Phase 12.4 Agent 工作记忆（Scratchpad）。

ReAct 循环每轮 tool_result 写回 messages 之前，调 Haiku 把它压缩成 1-2 条
"已知事实"推入 scratchpad；最近 1 轮保留 raw 数据，更早的 tool messages
内容被改写为占位（"参见已知事实"），下一轮 LLM 决策时看到的是：

    [system prompt]
    [已知事实摘要]      ← scratchpad 渲染段
    1. Alarm 16 = Master Link Down...
    2. Blue Button 控制 Manual Mode...

    [user 原始问题]
    [assistant: 思考1]
    [tool: 摘要占位]    ← raw 内容已被替换
    [assistant: 思考2]
    [tool: 摘要占位]
    [assistant: 思考3]
    [tool: RAW 最近一轮]  ← 最近一轮保留 raw
    [assistant: 思考N]

设计原则：
    - 失败一律降级：摘要失败 → 不推入 scratchpad、messages 不改写、
      继续走 raw 路径，主流程不阻塞
    - FIFO 上限：默认 10 条；超出删最早
    - 复用 reference_resolver 的 Haiku 调用封装风格（DB chat_models 路由）

env：
    ULTRARAG_SCRATCHPAD_ENABLED              默认 1（设 0 全跳，等同 Phase 12.4 前行为）
    ULTRARAG_SCRATCHPAD_MAX_FACTS            默认 10（FIFO 上限）
    ULTRARAG_SCRATCHPAD_KEEP_LATEST_N        默认 1（最近 N 轮 tool 保留 raw）
    ULTRARAG_SCRATCHPAD_BACKEND              anthropic | gemini（默认 anthropic）
    ULTRARAG_SCRATCHPAD_MODEL                默认 claude-haiku-4-5-20251001
    ULTRARAG_SCRATCHPAD_FALLBACK_MODEL       默认 gemini-2.0-flash
    ULTRARAG_SCRATCHPAD_TIMEOUT_SEC          默认 10
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompt"
_PROMPT_TEMPLATE_NAME = "agent_tool_summary.jinja"

DEFAULT_MAX_FACTS = 10
DEFAULT_KEEP_LATEST_N = 1
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
# Phase 12.4 default fallback: Google 2026 退役了 gemini-2.0-flash；用 DB
# 里你已配置的 gemini-3.1-pro-preview。可通过 env 覆盖。
DEFAULT_FALLBACK_MODEL = "gemini-3.1-pro-preview"
DEFAULT_TIMEOUT_SEC = 10
DEFAULT_MAX_FACT_CHARS = 200    # 单条 fact 硬截断（防 LLM 失控）
DEFAULT_MAX_INPUT_CHARS = 4000  # 喂给 Haiku 的 result 截断（防超长）

PLACEHOLDER_MESSAGE = (
    "[tool result moved to scratchpad summary at top of system prompt]"
)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class AgentScratchpad:
    """Agent 工作记忆容器。

    facts: 按时间顺序排列的事实列表；FIFO 满时删最早
    iteration_added_at: 每条 fact 是哪一轮添加的（前端可视化时间线用）
    total_summarize_ms: 累计所有 Haiku 调用耗时（audit / 性能监控用）
    """

    facts: list[str] = field(default_factory=list)
    iteration_added_at: list[int] = field(default_factory=list)
    total_summarize_ms: int = 0
    total_calls: int = 0

    def push(self, fact: str, iteration: int) -> None:
        """追加事实；满 max_facts 时删最早。"""
        max_facts = _env_int("ULTRARAG_SCRATCHPAD_MAX_FACTS", DEFAULT_MAX_FACTS)
        fact = (fact or "").strip()
        if not fact:
            return
        if len(fact) > DEFAULT_MAX_FACT_CHARS:
            fact = fact[:DEFAULT_MAX_FACT_CHARS].rstrip() + "..."
        self.facts.append(fact)
        self.iteration_added_at.append(int(iteration))
        # FIFO 删最早
        while len(self.facts) > max_facts:
            self.facts.pop(0)
            self.iteration_added_at.pop(0)

    def to_meta(self) -> dict[str, Any]:
        return {
            "facts": list(self.facts),
            "iteration_added_at": list(self.iteration_added_at),
            "total_summarize_ms": self.total_summarize_ms,
            "total_calls": self.total_calls,
            "size": len(self.facts),
        }


# ---------------------------------------------------------------------------
# env 工具
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool = True) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (ValueError, TypeError):
        return default


def is_enabled() -> bool:
    return _env_bool("ULTRARAG_SCRATCHPAD_ENABLED", default=True)


def keep_latest_n() -> int:
    """最近 N 轮 tool 保留 raw；其他被改写为占位。"""
    return max(0, _env_int("ULTRARAG_SCRATCHPAD_KEEP_LATEST_N", DEFAULT_KEEP_LATEST_N))


# ---------------------------------------------------------------------------
# Prompt 渲染
# ---------------------------------------------------------------------------


_template_cache: dict[str, Any] = {}


def _render_summary_prompt(tool_name: str, args: Any, result: Any) -> str:
    """渲染 agent_tool_summary.jinja。"""
    if "tmpl" not in _template_cache:
        from jinja2 import Environment, FileSystemLoader
        env = Environment(loader=FileSystemLoader(str(_PROMPT_DIR)))
        _template_cache["tmpl"] = env.get_template(_PROMPT_TEMPLATE_NAME)

    try:
        args_json = json.dumps(args, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        args_json = str(args)
    try:
        result_json = json.dumps(result, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        result_json = str(result)
    if len(result_json) > DEFAULT_MAX_INPUT_CHARS:
        result_json = result_json[:DEFAULT_MAX_INPUT_CHARS] + "...[truncated]"

    return _template_cache["tmpl"].render(
        tool_name=tool_name,
        tool_args_json=args_json,
        tool_result_json=result_json,
    )


def render_for_system_prompt(scratchpad: AgentScratchpad) -> str:
    """把 scratchpad 渲染成给 LLM 看的"已知事实"段；空 scratchpad 返回空串。

    Returns:
        "[已知事实摘要 — 共 N 条]\n1. fact1\n2. fact2\n..." 或 ""
    """
    if not scratchpad.facts:
        return ""
    lines = [f"[已知事实摘要 — 共 {len(scratchpad.facts)} 条，按发现顺序排列]"]
    for i, fact in enumerate(scratchpad.facts, start=1):
        lines.append(f"{i}. {fact}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM 调用（复用 reference_resolver 同款 DB chat_models 路由风格）
# ---------------------------------------------------------------------------


def _get_anthropic_model_from_db(preferred_model_name: str) -> dict[str, Any] | None:
    try:
        from custom_app.repositories.chat_model_repository import ChatModelRepository
        repo = ChatModelRepository()
        rows = repo.list_active(tenant_id=1, include_disabled=False)
    except Exception as e:  # noqa: BLE001
        logger.warning("ChatModelRepository unavailable: %s", e)
        return None
    anthropic_rows = [r for r in rows if (r.get("provider") or "").strip() == "anthropic"]
    if not anthropic_rows:
        return None
    for r in anthropic_rows:
        if (r.get("model_name") or "").strip() == preferred_model_name:
            return r
    return anthropic_rows[0]


def _call_anthropic(prompt: str, *, model: str, api_key: str,
                   timeout_sec: int) -> tuple[str, str]:
    try:
        from anthropic import Anthropic  # type: ignore
    except ImportError as e:
        raise RuntimeError("anthropic SDK not installed") from e

    actual_model = model
    actual_key = api_key
    db_row = _get_anthropic_model_from_db(model)
    if db_row:
        db_key = (db_row.get("api_key") or "").strip()
        db_model = (db_row.get("model_name") or "").strip()
        if db_key and db_model:
            actual_key = db_key
            actual_model = db_model

    if not actual_key:
        raise RuntimeError("ANTHROPIC_API_KEY missing (neither in DB nor env)")

    client = Anthropic(api_key=actual_key, timeout=timeout_sec)
    resp = client.messages.create(
        model=actual_model,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    parts: list[str] = []
    for block in resp.content or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts).strip(), actual_model


def _call_gemini(prompt: str, *, model: str, api_key: str,
                timeout_sec: int) -> tuple[str, str]:
    from custom_app.services.llm_adapter import (
        GeminiLLMAdapter,
        gemini_response_extract_text,
    )

    adapter = GeminiLLMAdapter(api_key=api_key, model=model, timeout=float(timeout_sec))
    resp = adapter.call(
        messages=[{"role": "user", "content": prompt}],
        generation_config={"temperature": 0.0, "maxOutputTokens": 400},
    )
    return gemini_response_extract_text(resp).strip(), model


def _parse_llm_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```\w*\n", "", t)
        t = re.sub(r"\n```\s*$", "", t)
        t = t.strip()
    try:
        return json.loads(t)
    except (json.JSONDecodeError, ValueError):
        pass
    m = re.search(r"\{[\s\S]*\}", t)
    if m:
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            pass
    return None


# ---------------------------------------------------------------------------
# 主入口：summarize_tool_result
# ---------------------------------------------------------------------------


def summarize_tool_result(
    *,
    tool_name: str,
    args: Any,
    result: Any,
) -> list[str]:
    """对一次工具调用结果做摘要，返回 1-2 条 fact；失败返回空列表。

    失败一律返回 []：调用方应判断 if facts then push to scratchpad，
    不要让摘要失败阻塞主流程。
    """
    if not is_enabled():
        return []
    if not tool_name:
        return []

    backend = (
        os.environ.get("ULTRARAG_SCRATCHPAD_BACKEND") or "anthropic"
    ).strip().lower()
    timeout_sec = max(1, _env_int(
        "ULTRARAG_SCRATCHPAD_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC,
    ))
    primary = (os.environ.get("ULTRARAG_SCRATCHPAD_MODEL") or DEFAULT_MODEL).strip()
    fallback = (
        os.environ.get("ULTRARAG_SCRATCHPAD_FALLBACK_MODEL") or DEFAULT_FALLBACK_MODEL
    ).strip()

    prompt = _render_summary_prompt(tool_name, args, result)

    raw_text = ""
    last_err: BaseException | None = None

    if backend == "anthropic":
        api_key = (
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ULTRARAG_ANTHROPIC_API_KEY")
            or ""
        ).strip()
        try:
            raw_text, _ = _call_anthropic(
                prompt, model=primary, api_key=api_key, timeout_sec=timeout_sec,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("scratchpad anthropic failed: %s", e)
            last_err = e

    if not raw_text:
        gemini_key = (
            os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("ULTRARAG_GEMINI_API_KEY")
            or ""
        ).strip()
        if not gemini_key:
            try:
                from custom_app.repositories.chat_model_repository import (
                    ChatModelRepository,
                )
                repo_cm = ChatModelRepository()
                gemini_rows = [
                    r for r in repo_cm.list_active(tenant_id=1)
                    if (r.get("provider") or "").strip() == "gemini"
                ]
                if gemini_rows:
                    gemini_key = (gemini_rows[0].get("api_key") or "").strip()
            except Exception as e:  # noqa: BLE001
                logger.debug("ChatModelRepository unavailable for fallback: %s", e)
        if gemini_key:
            try:
                raw_text, _ = _call_gemini(
                    prompt, model=fallback, api_key=gemini_key, timeout_sec=timeout_sec,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("scratchpad gemini fallback failed: %s", e)
                last_err = e

    if not raw_text:
        logger.warning("scratchpad LLM unavailable: %s", last_err)
        return []

    parsed = _parse_llm_json(raw_text)
    if not parsed or not isinstance(parsed, dict):
        logger.warning("scratchpad parse_error: %r", raw_text[:200])
        return []

    facts_raw = parsed.get("facts") or []
    if not isinstance(facts_raw, list):
        return []
    out: list[str] = []
    for item in facts_raw:
        s = str(item or "").strip()
        if s:
            out.append(s)
    return out


def summarize_and_push(
    scratchpad: AgentScratchpad,
    *,
    tool_name: str,
    args: Any,
    result: Any,
    iteration: int,
) -> list[str]:
    """便利函数：summarize + push + 累计耗时；返回本次新增的 facts（≤2 条）。

    调用方在每个 tool_result 之后调一次；失败不抛异常。
    """
    t0 = time.perf_counter()
    try:
        facts = summarize_tool_result(tool_name=tool_name, args=args, result=result)
    except Exception as e:  # noqa: BLE001
        logger.warning("summarize_and_push failed: %s", e)
        facts = []
    ms = int((time.perf_counter() - t0) * 1000)
    scratchpad.total_summarize_ms += ms
    scratchpad.total_calls += 1
    for f in facts:
        scratchpad.push(f, iteration=iteration)
    return facts


# ---------------------------------------------------------------------------
# messages 改写：把更早的 tool messages 内容置换为占位
# ---------------------------------------------------------------------------


def rewrite_old_tool_messages_in_place(messages: list[dict[str, Any]]) -> int:
    """把除最近 KEEP_LATEST_N 个 tool message 外的所有 tool message content
    替换为 PLACEHOLDER_MESSAGE，节省 token。

    Returns:
        被改写的 tool message 数量
    """
    keep_n = keep_latest_n()
    # 倒序找到最近 N 个 tool message 的索引（keep_n=0 时跳过这一步，全改写）
    tool_indices_reverse: list[int] = []
    if keep_n > 0:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "tool":
                tool_indices_reverse.append(i)
                if len(tool_indices_reverse) >= keep_n:
                    break
    kept = set(tool_indices_reverse)

    rewritten = 0
    for i, msg in enumerate(messages):
        if msg.get("role") != "tool":
            continue
        if i in kept:
            continue
        # 已被改写过的（identifiable by content marker）跳过
        content = msg.get("content")
        if isinstance(content, dict) and content.get("_scratchpad_placeholder"):
            continue
        msg["content"] = {
            "_scratchpad_placeholder": True,
            "_note": PLACEHOLDER_MESSAGE,
        }
        rewritten += 1
    return rewritten
