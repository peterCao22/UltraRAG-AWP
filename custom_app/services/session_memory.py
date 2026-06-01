"""Phase 12.2 Session Memory（长会话摘要）。

每 N 轮（N=10 默认）把前段对话压缩成 ≤200 字摘要 + 关键事实数组；
下一次 chat 时把 [summary] + [最近 K 轮] 一起拼进 prompt，
避免长会话历史超 token 上限或被截断丢前文。

设计原则（与 Phase 12.1 reference_resolver 对齐）：
    - 同步调用，不引后台任务 / 锁机制（MVP 简单可控）
    - LLM 失败降级：跳过本次摘要，保留旧 summary；不阻塞主对话
    - 全程不抛异常：任何错误返回 SummaryResult(applied=False, skip_reason=...)
    - 幂等：未达 N 轮直接返回 skip_reason="below_threshold"，零 token

env：
    ULTRARAG_SESSION_MEMORY_ENABLED            默认 1
    ULTRARAG_SESSION_MEMORY_WINDOW             默认 10（每攒满 N 轮新消息触发）
    ULTRARAG_SESSION_MEMORY_RECENT_K           默认 6（摘要后仍拼进 prompt 的最近 K 轮）
    ULTRARAG_SESSION_MEMORY_MAX_SUMMARY_CHARS  默认 600（摘要截断硬上限）
    ULTRARAG_SESSION_MEMORY_BACKEND            anthropic | gemini（默认 anthropic）
    ULTRARAG_SESSION_MEMORY_MODEL              默认 claude-haiku-4-5-20251001
    ULTRARAG_SESSION_MEMORY_FALLBACK_MODEL     默认 gemini-2.0-flash
    ULTRARAG_SESSION_MEMORY_TIMEOUT_SEC        默认 20
    ANTHROPIC_API_KEY / ULTRARAG_ANTHROPIC_API_KEY
    GOOGLE_API_KEY / ULTRARAG_GEMINI_API_KEY
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

from custom_app.db import now_iso
from custom_app.repositories.session_repository import SessionRepository

_logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompt"
_PROMPT_TEMPLATE_NAME = "session_summary.jinja"

# 默认值
DEFAULT_WINDOW = 10
DEFAULT_RECENT_K = 6
DEFAULT_MAX_SUMMARY_CHARS = 600
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_FALLBACK_MODEL = "gemini-2.0-flash"
DEFAULT_TIMEOUT_SEC = 20


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


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class SummaryResult:
    """maybe_summarize 的返回结果。"""

    applied: bool = False                  # True = 已写入新摘要；False = 跳过
    skip_reason: str | None = None         # disabled / no_session / below_threshold / error / parse_error
    summary: str = ""                      # 新写入的摘要文本（applied=True 时）
    facts: list[str] = field(default_factory=list)
    summary_at_msg_id: int = 0             # 本次摘要覆盖到的最后一条 msg.id
    model: str | None = None
    ms: int = 0

    def to_meta(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "skip_reason": self.skip_reason,
            "summary_chars": len(self.summary),
            "fact_count": len(self.facts),
            "summary_at_msg_id": self.summary_at_msg_id,
            "model": self.model,
            "ms": self.ms,
        }


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


_template_cache: dict[str, Any] = {}


def _render_prompt(history: list[dict[str, Any]], previous_summary: str) -> str:
    """渲染 session_summary.jinja。Jinja2 Environment 缓存避免重复加载。"""
    if "tmpl" not in _template_cache:
        from jinja2 import Environment, FileSystemLoader
        env = Environment(loader=FileSystemLoader(str(_PROMPT_DIR)))
        _template_cache["tmpl"] = env.get_template(_PROMPT_TEMPLATE_NAME)
    return _template_cache["tmpl"].render(
        history_text=_format_history(history),
        previous_summary=previous_summary.strip() or "（无）",
    )


def _format_history(
    history: list[dict[str, Any]], max_chars_per_turn: int = 800,
) -> str:
    """把 history 拼成 prompt 文本（最旧→最新）。"""
    if not history:
        return "（空）"
    lines = []
    for turn in history:
        role = (turn.get("role") or "").strip()
        content = (turn.get("content") or "").strip()
        if not content:
            continue
        if len(content) > max_chars_per_turn:
            content = content[:max_chars_per_turn] + "...[truncated]"
        role_label = {"user": "用户", "assistant": "助手"}.get(role, role)
        lines.append(f"{role_label}: {content}")
    return "\n".join(lines) if lines else "（空）"


def _parse_llm_json(text: str) -> dict[str, Any] | None:
    """从 LLM 输出抓 JSON（剥 ```json ... ``` 包裹）。失败返回 None。"""
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


def _truncate_summary(text: str, max_chars: int) -> str:
    """硬截断，避免 LLM 返回失控长摘要。"""
    s = (text or "").strip()
    if len(s) <= max_chars:
        return s
    return s[:max_chars].rstrip() + "..."


# ---------------------------------------------------------------------------
# LLM 调用（与 reference_resolver._call_anthropic / _call_gemini 同构）
# ---------------------------------------------------------------------------


def _get_anthropic_model_from_db(preferred_model_name: str) -> dict[str, Any] | None:
    """从 chat_models 表挑 anthropic 模型（admin 在后台配的 key）。"""
    try:
        from custom_app.repositories.chat_model_repository import ChatModelRepository
        repo = ChatModelRepository()
        rows = repo.list_active(tenant_id=1, include_disabled=False)
    except Exception as e:  # noqa: BLE001
        _logger.warning("ChatModelRepository unavailable: %s", e)
        return None
    anthropic_rows = [
        r for r in rows if (r.get("provider") or "").strip() == "anthropic"
    ]
    if not anthropic_rows:
        return None
    for r in anthropic_rows:
        if (r.get("model_name") or "").strip() == preferred_model_name:
            return r
    return anthropic_rows[0]


def _call_anthropic(
    history: list[dict[str, Any]],
    previous_summary: str,
    *,
    model: str,
    api_key: str,
    timeout_sec: int,
) -> tuple[str, str]:
    """调 Anthropic Haiku；返回 (raw_text, model_used)。失败抛异常。"""
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
    prompt = _render_prompt(history, previous_summary)
    resp = client.messages.create(
        model=actual_model,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    parts: list[str] = []
    for block in resp.content or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts).strip(), actual_model


def _call_gemini(
    history: list[dict[str, Any]],
    previous_summary: str,
    *,
    model: str,
    api_key: str,
    timeout_sec: int,
) -> tuple[str, str]:
    """fallback：Gemini。失败抛异常。"""
    from custom_app.services.llm_adapter import (
        GeminiLLMAdapter,
        gemini_response_extract_text,
    )

    adapter = GeminiLLMAdapter(api_key=api_key, model=model, timeout=float(timeout_sec))
    prompt = _render_prompt(history, previous_summary)
    resp = adapter.call(
        messages=[{"role": "user", "content": prompt}],
        generation_config={"temperature": 0.2, "maxOutputTokens": 800},
    )
    return gemini_response_extract_text(resp).strip(), model


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def _resolve_window() -> int:
    """读 ULTRARAG_SESSION_MEMORY_WINDOW；下限 2，避免 1 轮就触发。"""
    n = _env_int("ULTRARAG_SESSION_MEMORY_WINDOW", DEFAULT_WINDOW)
    return max(2, n)


def get_recent_k() -> int:
    """读 ULTRARAG_SESSION_MEMORY_RECENT_K；下限 1。供主流程拼 prompt 用。"""
    k = _env_int("ULTRARAG_SESSION_MEMORY_RECENT_K", DEFAULT_RECENT_K)
    return max(1, k)


def get_summary_for_prompt(session_id: str) -> str:
    """取该会话当前摘要文本（已截断 / 可能为空）；供 _prepare_chat_context 拼 prompt。"""
    if not session_id:
        return ""
    try:
        state = SessionRepository().get_summary_state(session_id)
    except Exception as e:  # noqa: BLE001 — DB 故障不阻塞主对话
        _logger.warning("get_summary_state failed session=%s: %s", session_id, e)
        return ""
    if not state:
        return ""
    return str(state.get("summary") or "").strip()


def maybe_summarize(session_id: str) -> SummaryResult:
    """若自上次摘要后已攒满 N 条新 message，调 LLM 生成新摘要并写库。

    设计约定：
        - "1 轮" 在我们的 schema 里实际是 2 条 message（user + assistant）
        - WINDOW 单位是 message 条数，与 schema 直接对齐；默认 10 = 5 轮
        - 同步调用，调完才返回；主对话需在生成回答**写库之后**调用本函数
    """
    t0 = time.perf_counter()
    result = SummaryResult()

    if not _env_bool("ULTRARAG_SESSION_MEMORY_ENABLED", default=True):
        result.skip_reason = "disabled"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    sid = (session_id or "").strip()
    if not sid:
        result.skip_reason = "no_session"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    window = _resolve_window()
    max_summary_chars = _env_int(
        "ULTRARAG_SESSION_MEMORY_MAX_SUMMARY_CHARS", DEFAULT_MAX_SUMMARY_CHARS,
    )

    repo = SessionRepository()
    try:
        state = repo.get_summary_state(sid)
    except Exception as e:  # noqa: BLE001
        _logger.warning("get_summary_state failed session=%s: %s", sid, e)
        result.skip_reason = f"error:{type(e).__name__}"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    if state is None:
        result.skip_reason = "no_session"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    previous_summary = str(state.get("summary") or "").strip()
    last_msg_id = int(state.get("summary_at_msg_id") or 0)

    # 取该 session 全部 message + 算 delta_count
    try:
        all_msgs = repo.list_messages(sid)
    except Exception as e:  # noqa: BLE001
        _logger.warning("list_messages failed session=%s: %s", sid, e)
        result.skip_reason = f"error:{type(e).__name__}"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    new_msgs = [m for m in all_msgs if int(m.get("id") or 0) > last_msg_id]
    if len(new_msgs) < window:
        result.skip_reason = "below_threshold"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    # 候选摘要素材：上次摘要以来的所有新消息
    history_for_summary = [
        {"role": m.get("role"), "content": m.get("content")}
        for m in new_msgs
    ]
    new_max_msg_id = int(new_msgs[-1].get("id") or last_msg_id)

    # 选 backend
    backend = (
        os.environ.get("ULTRARAG_SESSION_MEMORY_BACKEND") or "anthropic"
    ).strip().lower()
    timeout_sec = max(
        1, _env_int("ULTRARAG_SESSION_MEMORY_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC),
    )
    model_primary = (
        os.environ.get("ULTRARAG_SESSION_MEMORY_MODEL") or DEFAULT_MODEL
    ).strip()
    model_fallback = (
        os.environ.get("ULTRARAG_SESSION_MEMORY_FALLBACK_MODEL")
        or DEFAULT_FALLBACK_MODEL
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
                history_for_summary, previous_summary,
                model=model_primary, api_key=api_key, timeout_sec=timeout_sec,
            )
        except Exception as e:  # noqa: BLE001
            _logger.warning("session_memory anthropic failed: %s", e)
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
                _logger.debug("ChatModelRepository unavailable for fallback: %s", e)
        if gemini_key:
            try:
                raw_text, model_used = _call_gemini(
                    history_for_summary, previous_summary,
                    model=model_fallback, api_key=gemini_key,
                    timeout_sec=timeout_sec,
                )
            except Exception as e:  # noqa: BLE001
                _logger.warning("session_memory gemini fallback failed: %s", e)
                last_err = e

    if not raw_text:
        result.skip_reason = (
            f"error:{type(last_err).__name__}"
            if last_err else "no_backend_available"
        )
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    parsed = _parse_llm_json(raw_text)
    if not parsed or not isinstance(parsed, dict):
        result.skip_reason = "parse_error"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    summary_text = _truncate_summary(
        str(parsed.get("summary") or ""), max_summary_chars,
    )
    if not summary_text:
        result.skip_reason = "empty_summary"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    facts_raw = parsed.get("facts") or []
    facts: list[str] = []
    if isinstance(facts_raw, list):
        for item in facts_raw:
            s = str(item or "").strip()
            if s:
                facts.append(s[:200])  # 单条事实硬截断
    # facts 与 summary 一起拼回 summary 文本（用 prompt 时只读 summary 字段，
    # 但 facts 信息已含在生成的 summary 里；这里把 facts 多带回是给上层 UI/调试用）

    # 写库
    try:
        repo.update_summary(
            sid,
            summary=summary_text,
            summary_at_msg_id=new_max_msg_id,
            summary_updated_at=now_iso(),
        )
    except Exception as e:  # noqa: BLE001
        _logger.warning("update_summary failed session=%s: %s", sid, e)
        result.skip_reason = f"error:{type(e).__name__}"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    result.applied = True
    result.summary = summary_text
    result.facts = facts
    result.summary_at_msg_id = new_max_msg_id
    result.model = model_used
    result.ms = int((time.perf_counter() - t0) * 1000)
    _logger.info(
        "session_memory summarized session=%s msgs_summarized=%d "
        "summary_chars=%d facts=%d model=%s ms=%d",
        sid, len(new_msgs), len(summary_text), len(facts), model_used, result.ms,
    )
    return result
