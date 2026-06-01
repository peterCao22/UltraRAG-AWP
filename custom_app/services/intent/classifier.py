"""Phase 11.1.5 Query 意图分类器：规则优先 + Haiku fallback。

四类意图：
    - chitchat:    闲聊（你好/谢谢/再见）→ 模板回复，跳 RAG
    - help:        元问题（你能做什么/帮助）→ 模板回复，跳 RAG
    - data_query:  数据查询（统计/有多少/最近 N 天）→ 模板"开发中"，跳 RAG
    - knowledge:   知识问答 → 走 RAG（默认）

设计原则：
    - 规则优先：常见短问候 zero-token zero-latency
    - LLM 兜底：规则不确定时才调 Haiku（与 reference_resolver / session_memory
      同模型同 backend，省去再独立配 key）
    - 失败一律降级到 knowledge：宁可走一次 RAG，不要因分类挂掉拒绝用户

env：
    ULTRARAG_INTENT_ENABLED              默认 1（设 0 全跳 LLM、全走 knowledge）
    ULTRARAG_INTENT_LLM_FALLBACK         默认 1（设 0 规则不命中也走 knowledge，零 token）
    ULTRARAG_INTENT_LLM_BACKEND          anthropic | gemini（默认 anthropic）
    ULTRARAG_INTENT_LLM_MODEL            默认 claude-haiku-4-5-20251001
    ULTRARAG_INTENT_LLM_FALLBACK_MODEL   默认 gemini-2.0-flash
    ULTRARAG_INTENT_LLM_TIMEOUT_SEC      默认 8
    ULTRARAG_INTENT_MIN_CONFIDENCE       默认 0.5（LLM 置信度低于此值忽略 LLM 结果走 knowledge）
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


INTENT_CHITCHAT = "chitchat"
INTENT_HELP = "help"
INTENT_DATA_QUERY = "data_query"
INTENT_KNOWLEDGE = "knowledge"

VALID_INTENTS = frozenset({
    INTENT_CHITCHAT, INTENT_HELP, INTENT_DATA_QUERY, INTENT_KNOWLEDGE,
})

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_FALLBACK_MODEL = "gemini-2.0-flash"
DEFAULT_TIMEOUT_SEC = 8
DEFAULT_MIN_CONFIDENCE = 0.5

# Prompt 模板路径
_PROMPT_DIR = Path(__file__).resolve().parent.parent.parent.parent / "prompt"
_PROMPT_TEMPLATE_NAME = "intent_classification.jinja"


# ---------------------------------------------------------------------------
# 规则层
# ---------------------------------------------------------------------------

# chitchat：含问候/告别/感谢词的短句
_CHITCHAT_KEYWORDS = (
    "你好", "您好", "早上好", "下午好", "晚上好", "嗨", "哈喽",
    "再见", "拜拜", "回头见", "谢谢", "感谢", "thank", "thanks",
    "hello", "hi", "hey", "bye", "good morning", "good night",
)

# help：明确询问系统能力 / 介绍
_HELP_PATTERNS = (
    re.compile(r"你能(做|干|帮我做|帮我)?(什么|啥|哪些)", re.IGNORECASE),
    re.compile(r"你会(做|干|什么|啥)", re.IGNORECASE),
    re.compile(r"(怎么|如何|怎样)\s*用\s*(你|这个|系统)"),
    re.compile(r"你是(谁|什么)"),
    re.compile(r"^\s*(帮助|help)\s*[?？]?\s*$", re.IGNORECASE),
    re.compile(r"使用说明|用户手册"),
    re.compile(r"\bwhat can you (do|help)\b", re.IGNORECASE),
    re.compile(r"\bwho are you\b", re.IGNORECASE),
    re.compile(r"\bhow (do|to|can) i use (this|you|the system)\b", re.IGNORECASE),
)

# data_query：明确的数据统计、查询业务系统
_DATA_QUERY_PATTERNS = (
    re.compile(r"统计|有多少|总(共|计)\s*(多少|几)|累计"),
    re.compile(r"最近\s*\d+\s*(天|周|月)"),
    re.compile(r"昨天|今天|本月|上月|本周|上周"),
    re.compile(r"故障次数|故障频次|故障率"),
    re.compile(r"哪些设备|哪台设备|哪个 ?AGV"),
    re.compile(r"\bhow many\b", re.IGNORECASE),
    re.compile(r"\btotal\s+(count|number)\b", re.IGNORECASE),
    re.compile(r"\b(last|past)\s+\d+\s+(days|weeks|months)\b", re.IGNORECASE),
)


@dataclass
class IntentResult:
    """意图分类结果。"""

    intent: str = INTENT_KNOWLEDGE
    confidence: float = 1.0
    source: str = "rule"  # "rule" | "llm" | "fallback" | "disabled"
    ms: int = 0
    raw_llm_text: str | None = None  # 调试用

    def to_meta(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "ms": self.ms,
        }


# ---------------------------------------------------------------------------
# env 读取
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


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# 规则分类
# ---------------------------------------------------------------------------


def _classify_by_rules(query: str) -> tuple[str | None, float]:
    """按规则做高 precision 命中；不确定返回 (None, 0.0) 让 LLM 兜底。

    规则只识别"非常确定"的场景：
        - 极短 + 含明确 chitchat keyword
        - 明确 pattern 的 help / data_query
    其余一律返回 None 让 LLM 处理。
    """
    if not query:
        return None, 0.0
    q = query.strip()
    q_lower = q.lower()

    # data_query 模式（优先于 help / chitchat，因为常包含"几"/"什么"等词）
    for p in _DATA_QUERY_PATTERNS:
        if p.search(q):
            return INTENT_DATA_QUERY, 0.9

    # help 模式
    for p in _HELP_PATTERNS:
        if p.search(q):
            return INTENT_HELP, 0.9

    # chitchat：含问候词 + 短句（≤ 12 字）
    # 避免误判"hello world 程序怎么写"这种问候词嵌入的知识问题
    if len(q) <= 12:
        for k in _CHITCHAT_KEYWORDS:
            if k in q_lower or k in q:
                return INTENT_CHITCHAT, 0.85

    return None, 0.0


# ---------------------------------------------------------------------------
# LLM 分类（兜底）
# ---------------------------------------------------------------------------


_template_cache: dict[str, Any] = {}


def _render_prompt(query: str) -> str:
    if "tmpl" not in _template_cache:
        from jinja2 import Environment, FileSystemLoader
        env = Environment(loader=FileSystemLoader(str(_PROMPT_DIR)))
        _template_cache["tmpl"] = env.get_template(_PROMPT_TEMPLATE_NAME)
    return _template_cache["tmpl"].render(query=query)


def _parse_llm_json(text: str) -> dict[str, Any] | None:
    """从 LLM 输出抓 JSON。"""
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


def _get_anthropic_model_from_db(preferred_model_name: str) -> dict[str, Any] | None:
    """与 reference_resolver / session_memory 一致：admin 后台配的 Anthropic 模型行优先。"""
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


def _call_anthropic(query: str, *, model: str, api_key: str,
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
    prompt = _render_prompt(query)
    resp = client.messages.create(
        model=actual_model,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    parts: list[str] = []
    for block in resp.content or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts).strip(), actual_model


def _call_gemini(query: str, *, model: str, api_key: str,
                timeout_sec: int) -> tuple[str, str]:
    from custom_app.services.llm_adapter import (
        GeminiLLMAdapter,
        gemini_response_extract_text,
    )

    adapter = GeminiLLMAdapter(api_key=api_key, model=model, timeout=float(timeout_sec))
    prompt = _render_prompt(query)
    resp = adapter.call(
        messages=[{"role": "user", "content": prompt}],
        generation_config={"temperature": 0.0, "maxOutputTokens": 200},
    )
    return gemini_response_extract_text(resp).strip(), model


def _classify_by_llm(query: str) -> tuple[str | None, float, str, str | None]:
    """LLM 分类。返回 (intent | None, confidence, model_used, raw_text)。"""
    backend = (
        os.environ.get("ULTRARAG_INTENT_LLM_BACKEND") or "anthropic"
    ).strip().lower()
    timeout_sec = max(1, _env_int("ULTRARAG_INTENT_LLM_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC))
    primary = (os.environ.get("ULTRARAG_INTENT_LLM_MODEL") or DEFAULT_MODEL).strip()
    fallback = (
        os.environ.get("ULTRARAG_INTENT_LLM_FALLBACK_MODEL") or DEFAULT_FALLBACK_MODEL
    ).strip()

    raw_text = ""
    model_used = ""
    last_err: BaseException | None = None

    if backend == "anthropic":
        api_key = (
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ULTRARAG_ANTHROPIC_API_KEY")
            or ""
        ).strip()
        try:
            raw_text, model_used = _call_anthropic(
                query, model=primary, api_key=api_key, timeout_sec=timeout_sec,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("intent anthropic failed: %s", e)
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
                repo = ChatModelRepository()
                gemini_rows = [
                    r for r in repo.list_active(tenant_id=1)
                    if (r.get("provider") or "").strip() == "gemini"
                ]
                if gemini_rows:
                    gemini_key = (gemini_rows[0].get("api_key") or "").strip()
            except Exception as e:  # noqa: BLE001
                logger.debug("ChatModelRepository unavailable for gemini fallback: %s", e)
        if gemini_key:
            try:
                raw_text, model_used = _call_gemini(
                    query, model=fallback, api_key=gemini_key, timeout_sec=timeout_sec,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("intent gemini fallback failed: %s", e)
                last_err = e

    if not raw_text:
        return None, 0.0, model_used, None

    parsed = _parse_llm_json(raw_text)
    if not parsed or not isinstance(parsed, dict):
        return None, 0.0, model_used, raw_text

    intent = str(parsed.get("intent") or "").strip().lower()
    if intent not in VALID_INTENTS:
        return None, 0.0, model_used, raw_text
    confidence = float(parsed.get("confidence") or 0.0)
    return intent, confidence, model_used, raw_text


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def classify_intent(query: str) -> IntentResult:
    """对单个 query 做意图分类；任何异常都退化为 knowledge（走 RAG）。

    流程：
        1. ULTRARAG_INTENT_ENABLED=0 → 直接 knowledge (source='disabled')
        2. 规则命中 → 直接返回 (source='rule')
        3. ULTRARAG_INTENT_LLM_FALLBACK=0 → knowledge (source='fallback')
        4. LLM 分类 → 通过置信度阈值则返回 (source='llm')
        5. LLM 失败或置信度低 → knowledge (source='fallback')
    """
    t0 = time.perf_counter()

    if not _env_bool("ULTRARAG_INTENT_ENABLED", default=True):
        return IntentResult(
            intent=INTENT_KNOWLEDGE, confidence=1.0,
            source="disabled", ms=int((time.perf_counter() - t0) * 1000),
        )

    q = (query or "").strip()
    if not q:
        return IntentResult(
            intent=INTENT_KNOWLEDGE, confidence=1.0,
            source="fallback", ms=int((time.perf_counter() - t0) * 1000),
        )

    # 规则层
    rule_intent, rule_conf = _classify_by_rules(q)
    if rule_intent is not None:
        return IntentResult(
            intent=rule_intent, confidence=rule_conf,
            source="rule", ms=int((time.perf_counter() - t0) * 1000),
        )

    # LLM 兜底
    if not _env_bool("ULTRARAG_INTENT_LLM_FALLBACK", default=True):
        return IntentResult(
            intent=INTENT_KNOWLEDGE, confidence=1.0,
            source="fallback", ms=int((time.perf_counter() - t0) * 1000),
        )

    min_conf = _env_float("ULTRARAG_INTENT_MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE)
    try:
        llm_intent, llm_conf, model_used, raw = _classify_by_llm(q)
    except Exception as e:  # noqa: BLE001 — LLM 异常一律降级
        logger.warning("intent llm failed: %s", e)
        llm_intent, llm_conf, raw = None, 0.0, None

    ms = int((time.perf_counter() - t0) * 1000)
    if llm_intent is None or llm_conf < min_conf:
        # 失败或低置信度：走 RAG 总比误判好
        return IntentResult(
            intent=INTENT_KNOWLEDGE, confidence=1.0,
            source="fallback", ms=ms, raw_llm_text=raw,
        )

    return IntentResult(
        intent=llm_intent, confidence=llm_conf,
        source="llm", ms=ms, raw_llm_text=raw,
    )
