"""Phase 12.1 Context Resolution（指代消解）。

让用户能用"它/这个/第 N 个/继续"这种指代型问题继续对话，系统基于历史
回答把指代消解成具体内容，再走 RAG。

两层设计：
    1. 规则检测（has_reference_marker）：纯函数，识别中/英指代词，0 token 成本
    2. LLM 改写（resolve_with_llm）：仅当规则触发 + 有历史时调用 Claude Haiku 4.5

env：
    ULTRARAG_REF_RESOLUTION_ENABLED            默认 1
    ULTRARAG_REF_RESOLUTION_BACKEND            anthropic | gemini（默认 anthropic）
    ULTRARAG_REF_RESOLUTION_MODEL              默认 claude-haiku-4-5-20251001
    ULTRARAG_REF_RESOLUTION_FALLBACK_MODEL     默认 gemini-2.0-flash
    ULTRARAG_REF_RESOLUTION_MIN_CONFIDENCE     默认 0.7
    ULTRARAG_REF_RESOLUTION_MAX_HISTORY        默认 6
    ANTHROPIC_API_KEY / ULTRARAG_ANTHROPIC_API_KEY
    GOOGLE_API_KEY / ULTRARAG_GEMINI_API_KEY（fallback 用）
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

_logger = logging.getLogger(__name__)

# Prompt 模板路径（与项目其它 prompt 一致放在 prompt/ 下）
_PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompt"
_PROMPT_TEMPLATE_NAME = "reference_resolution.jinja"


# ────────────────────────────────────────────────────────────────────────────
# 配置（env 读取，模块级缓存）
# ────────────────────────────────────────────────────────────────────────────

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_FALLBACK_MODEL = "gemini-2.0-flash"
DEFAULT_MIN_CONFIDENCE = 0.7
DEFAULT_MAX_HISTORY = 6
DEFAULT_TIMEOUT_SEC = 15


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


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (ValueError, TypeError):
        return default


# ────────────────────────────────────────────────────────────────────────────
# 规则层：指代词检测（中英文）
# ────────────────────────────────────────────────────────────────────────────

# 中文代词 + 序数 + 续问。匹配时用 contains/regex，避免误命中（如 "继续教育"）。
# 设计原则：宁可漏判走 LLM，不可误判触发改写。
_CN_PRONOUNS = (
    "它", "他", "她", "它们", "他们", "她们",
    "这个", "那个", "这些", "那些",
)
# 序数：第N / 第N个 / 第N步
_CN_ORDINAL_RE = re.compile(
    r"第\s*[一二三四五六七八九十百千]+\s*[个步项条款节段轮]?|"
    r"第\s*\d+\s*[个步项条款节段轮]?",
)
# 续问类
_CN_CONTINUATION = (
    "继续", "然后呢", "接着", "下文",
    "之后呢", "下一个", "下一步", "上一步", "上面", "下面",
    "那步", "这步",
)
# 英文常见指代/续问
_EN_REFERENCE = (
    " it ", " its ", " this ", " that ", " these ", " those ",
    " continue", " next step", " previous step", "what about",
)
_EN_ORDINAL_RE = re.compile(
    r"\bthe\s+(first|second|third|fourth|fifth|\d+(st|nd|rd|th))\b",
    re.IGNORECASE,
)


def has_reference_marker(query: str) -> bool:
    """规则检测：query 是否含指代/续问关键词。

    设计目标：召回率优先（漏判走原 query，错判触发不必要的 LLM 调用更糟）。
    返回 True 时，再交给 LLM 改写；False 时直接跳过改写，节省成本。
    """
    if not query:
        return False
    q = query.strip()
    if not q:
        return False

    # 中文代词：直接 substring（用前后空白/标点防止部分匹配）
    for p in _CN_PRONOUNS:
        if p in q:
            return True
    # 中文续问
    for p in _CN_CONTINUATION:
        if p in q:
            return True
    # 中文序数
    if _CN_ORDINAL_RE.search(q):
        return True
    # 英文指代：用前后空格防误判（如 "transition" 不命中）
    q_padded = " " + q.lower() + " "
    for p in _EN_REFERENCE:
        if p in q_padded:
            return True
    if _EN_ORDINAL_RE.search(q):
        return True

    return False


# ────────────────────────────────────────────────────────────────────────────
# 数据类
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class ResolutionResult:
    """指代消解结果。"""

    applied: bool = False               # 是否实际改写（False = 用原 query）
    original_query: str = ""
    rewritten_query: str = ""           # applied=False 时 == original_query
    confidence: float = 0.0
    resolved: list[dict[str, str]] = field(default_factory=list)  # [{reference, meaning}]
    ms: int = 0
    skip_reason: str | None = None      # no_history / no_marker / low_confidence / error / disabled
    model: str | None = None            # 实际用的模型
    raw_llm_text: str | None = None     # debug 用

    def to_meta(self) -> dict[str, Any]:
        """前端 SSE / meta 用的精简形式。"""
        return {
            "applied": self.applied,
            "original_query": self.original_query,
            "rewritten_query": self.rewritten_query,
            "confidence": round(self.confidence, 3),
            "resolved": self.resolved,
            "ms": self.ms,
            "skip_reason": self.skip_reason,
            "model": self.model,
        }


# ────────────────────────────────────────────────────────────────────────────
# LLM 改写
# ────────────────────────────────────────────────────────────────────────────

_template_cache: dict[str, Any] = {}


def _render_prompt(question: str, history: list[dict[str, Any]]) -> str:
    """渲染 prompt/reference_resolution.jinja 模板。

    使用 Jinja2 Environment 缓存避免重复加载。
    """
    if "tmpl" not in _template_cache:
        from jinja2 import Environment, FileSystemLoader
        env = Environment(loader=FileSystemLoader(str(_PROMPT_DIR)))
        _template_cache["tmpl"] = env.get_template(_PROMPT_TEMPLATE_NAME)
    return _template_cache["tmpl"].render(
        history_text=_format_history(history),
        question=question,
    )


def _format_history(history: list[dict[str, Any]], max_chars_per_turn: int = 600) -> str:
    """把 history 拼成 prompt 用的文本。

    history 期望格式：[{"role": "user"|"assistant", "content": "..."}, ...]
    最旧→最新。每条 content 长度限制避免 prompt 爆炸。
    """
    if not history:
        return "（无历史）"
    lines = []
    for turn in history:
        role = (turn.get("role") or "").strip()
        content = (turn.get("content") or "").strip()
        if not content:
            continue
        if len(content) > max_chars_per_turn:
            content = content[:max_chars_per_turn] + "...[truncated]"
        # 用户友好的 role 标签
        role_label = {"user": "用户", "assistant": "助手"}.get(role, role)
        lines.append(f"{role_label}: {content}")
    return "\n".join(lines) if lines else "（无有效历史）"


def _parse_llm_json(text: str) -> dict[str, Any] | None:
    """从 LLM 返回中抽 JSON。容错：剥代码块/前后空白。返回 None 表示解析失败。"""
    if not text:
        return None
    t = text.strip()
    # 剥 ```json ... ``` 包裹
    if t.startswith("```"):
        # 去首行 ``` 或 ```json
        t = re.sub(r"^```\w*\n", "", t)
        t = re.sub(r"\n```\s*$", "", t)
        t = t.strip()
    # 尝试直接 parse
    try:
        return json.loads(t)
    except (json.JSONDecodeError, ValueError):
        pass
    # 容错：抓首个 {...}
    m = re.search(r"\{[\s\S]*\}", t)
    if m:
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _get_anthropic_model_from_db(preferred_model_name: str) -> dict[str, Any] | None:
    """从 chat_models 表里挑一个可用的 Anthropic 模型。

    优先级：
        1. 显式指定的 model_name（如 'claude-haiku-4-5-20251001'）
        2. 任一启用的 anthropic 模型（取第一条）
    返回 model_row dict 或 None（DB 里没有 anthropic 模型）。
    """
    try:
        from custom_app.repositories.chat_model_repository import ChatModelRepository
        repo = ChatModelRepository()
        rows = repo.list_active(tenant_id=1, include_disabled=False)
    except Exception as e:  # noqa: BLE001 - DB 不可用时静默降级
        _logger.warning("ChatModelRepository unavailable: %s", e)
        return None
    anthropic_rows = [r for r in rows if (r.get("provider") or "").strip() == "anthropic"]
    if not anthropic_rows:
        return None
    # 优先匹配 preferred_model_name
    for r in anthropic_rows:
        if (r.get("model_name") or "").strip() == preferred_model_name:
            return r
    # 没匹配上，返回第一条 enabled 的
    return anthropic_rows[0]


def _call_anthropic(
    question: str,
    history: list[dict[str, Any]],
    *,
    model: str,
    api_key: str,
    timeout_sec: int,
) -> tuple[str, str]:
    """调 Anthropic Claude 改写；返回 (raw_text, model_used)。

    优先级：
        1. DB 里 chat_models 表中 provider=anthropic 的模型条目（admin 后台已配 key）
        2. env ANTHROPIC_API_KEY（fallback）
    若 DB 拿到的模型与 preferred_model 不同名，会使用 DB 里的（admin 配什么用什么）。

    失败抛异常，由上层捕获并 fallback 到 Gemini。
    """
    try:
        from anthropic import Anthropic  # type: ignore
    except ImportError as e:
        raise RuntimeError("anthropic SDK not installed") from e

    # 优先尝试 DB 模型条目
    actual_model = model
    actual_key = api_key
    db_row = _get_anthropic_model_from_db(model)
    if db_row:
        db_key = (db_row.get("api_key") or "").strip()
        db_model = (db_row.get("model_name") or "").strip()
        if db_key and db_model:
            actual_key = db_key
            actual_model = db_model
            _logger.debug(
                "reference resolver using DB anthropic model_id=%s model=%s",
                db_row.get("model_id"), actual_model,
            )

    if not actual_key:
        raise RuntimeError("ANTHROPIC_API_KEY missing (neither in DB nor env)")

    client = Anthropic(api_key=actual_key, timeout=timeout_sec)
    prompt = _render_prompt(question, history)
    resp = client.messages.create(
        model=actual_model,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    # 提取文本
    parts: list[str] = []
    for block in resp.content or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts).strip(), actual_model


def _call_gemini(
    question: str,
    history: list[dict[str, Any]],
    *,
    model: str,
    api_key: str,
    timeout_sec: int,
) -> tuple[str, str]:
    """fallback：调 Gemini 改写。失败抛异常。"""
    # 复用项目里已有的 GeminiLLMAdapter，避免引新依赖
    from custom_app.services.llm_adapter import (
        GeminiLLMAdapter,
        gemini_response_extract_text,
    )

    adapter = GeminiLLMAdapter(api_key=api_key, model=model, timeout=float(timeout_sec))
    prompt = _render_prompt(question, history)
    resp = adapter.call(
        messages=[{"role": "user", "content": prompt}],
        generation_config={"temperature": 0.2, "maxOutputTokens": 500},
    )
    return gemini_response_extract_text(resp).strip(), model


# ────────────────────────────────────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────────────────────────────────────


def resolve_references(
    question: str,
    history: list[dict[str, Any]] | None,
) -> ResolutionResult:
    """对外主接口：返回 ResolutionResult。

    Args:
        question: 当前用户 query
        history: 已落库的历史轮次（最旧→最新）；None / 空 视为单轮对话

    Returns:
        ResolutionResult。不抛异常；任何错误都退化为 applied=False + skip_reason。
    """
    t0 = time.perf_counter()
    original = (question or "").strip()
    result = ResolutionResult(
        original_query=original,
        rewritten_query=original,
    )

    # 全局禁用
    if not _env_bool("ULTRARAG_REF_RESOLUTION_ENABLED", default=True):
        result.skip_reason = "disabled"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    # 空 query
    if not original:
        result.skip_reason = "empty_query"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    # 无历史 → 没有上下文可参考，直接跳过
    history = history or []
    if not history:
        result.skip_reason = "no_history"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    # 规则检测：无指代词 → 跳过 LLM 调用
    if not has_reference_marker(original):
        result.skip_reason = "no_marker"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    # 截断历史
    max_history = max(1, _env_int("ULTRARAG_REF_RESOLUTION_MAX_HISTORY", DEFAULT_MAX_HISTORY))
    history = history[-max_history:]

    # 选 backend + 调 LLM
    backend = (os.environ.get("ULTRARAG_REF_RESOLUTION_BACKEND") or "anthropic").strip().lower()
    timeout_sec = max(1, _env_int("ULTRARAG_REF_RESOLUTION_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC))
    model_primary = (
        os.environ.get("ULTRARAG_REF_RESOLUTION_MODEL") or DEFAULT_MODEL
    ).strip()
    model_fallback = (
        os.environ.get("ULTRARAG_REF_RESOLUTION_FALLBACK_MODEL") or DEFAULT_FALLBACK_MODEL
    ).strip()
    min_conf = _env_float("ULTRARAG_REF_RESOLUTION_MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE)

    raw_text = ""
    model_used = ""
    last_err: BaseException | None = None

    # 主 backend：anthropic（key 来源优先级：env → DB chat_models 表）
    if backend == "anthropic":
        api_key = (
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ULTRARAG_ANTHROPIC_API_KEY")
            or ""
        ).strip()
        # 即便 env 没 key，_call_anthropic 内部也会查 DB；任由它处理
        try:
            raw_text, model_used = _call_anthropic(
                original, history, model=model_primary,
                api_key=api_key, timeout_sec=timeout_sec,
            )
        except Exception as e:  # noqa: BLE001 - 降级处理
            _logger.warning("reference resolver anthropic failed: %s", e)
            last_err = e

    # fallback 到 Gemini
    if not raw_text:
        # Gemini fallback：先 env，再 DB（chat_models 表 provider=gemini）
        gemini_key = (
            os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("ULTRARAG_GEMINI_API_KEY")
            or ""
        ).strip()
        if not gemini_key:
            try:
                from custom_app.repositories.chat_model_repository import ChatModelRepository
                repo = ChatModelRepository()
                gemini_rows = [
                    r for r in repo.list_active(tenant_id=1)
                    if (r.get("provider") or "").strip() == "gemini"
                ]
                if gemini_rows:
                    gemini_key = (gemini_rows[0].get("api_key") or "").strip()
            except Exception as e:  # noqa: BLE001
                _logger.debug("ChatModelRepository unavailable for gemini fallback: %s", e)
        if gemini_key:
            try:
                raw_text, model_used = _call_gemini(
                    original, history, model=model_fallback,
                    api_key=gemini_key, timeout_sec=timeout_sec,
                )
            except Exception as e:  # noqa: BLE001
                _logger.warning("reference resolver gemini fallback failed: %s", e)
                last_err = e

    if not raw_text:
        result.skip_reason = f"error:{type(last_err).__name__}" if last_err else "no_backend_available"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    result.raw_llm_text = raw_text
    result.model = model_used

    # 解析 JSON
    parsed = _parse_llm_json(raw_text)
    if not parsed or not isinstance(parsed, dict):
        result.skip_reason = "parse_error"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    rewritten = str(parsed.get("rewritten_query") or "").strip()
    confidence = float(parsed.get("confidence") or 0.0)
    resolved_list = parsed.get("resolved") or []
    if not isinstance(resolved_list, list):
        resolved_list = []
    # 过滤 resolved 项
    cleaned_resolved: list[dict[str, str]] = []
    for item in resolved_list:
        if isinstance(item, dict):
            ref = str(item.get("reference") or "").strip()
            meaning = str(item.get("meaning") or "").strip()
            if ref and meaning:
                cleaned_resolved.append({"reference": ref, "meaning": meaning})

    result.confidence = confidence
    result.resolved = cleaned_resolved

    # confidence 不足 → 不采纳改写
    if confidence < min_conf or not rewritten:
        result.skip_reason = "low_confidence"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    # 改写与原 query 完全一致 → 无意义改写
    if rewritten == original:
        result.skip_reason = "no_change"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    # 改写采纳
    result.applied = True
    result.rewritten_query = rewritten
    result.ms = int((time.perf_counter() - t0) * 1000)
    _logger.info(
        "reference_resolution applied confidence=%.2f model=%s ms=%d "
        "original=%r rewritten=%r",
        confidence, model_used, result.ms, original, rewritten,
    )
    return result
