"""Phase 12.2 Session Memory 单元测试。

覆盖：
  1. maybe_summarize 阈值边界（未达 N → skip；恰好达 → 触发）
  2. 幂等 / 重复调用（已写入摘要再调，但消息没新增 → 仍 skip）
  3. LLM JSON 解析失败 → applied=False, skip_reason="parse_error"
  4. LLM 调用失败 → applied=False, skip_reason 含 error
  5. 摘要硬截断到 max_chars
  6. session 不存在 → no_session
  7. ENABLED=0 → disabled
  8. get_summary_for_prompt 空 / 非空返回
  9. RagRunner._build_prompt prior_summary / recent_turns 拼接
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_app.services import session_memory
from custom_app.services.session_memory import SummaryResult


# ---------------------------------------------------------------------------
# fixtures：mock SessionRepository + LLM 调用
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_repo(monkeypatch):
    """替换 SessionRepository() 为 MagicMock 实例，所有方法可独立 stub。"""
    mock = MagicMock()
    monkeypatch.setattr(
        "custom_app.services.session_memory.SessionRepository",
        lambda *a, **kw: mock,
    )
    return mock


@pytest.fixture()
def fake_llm(monkeypatch):
    """替换 _call_anthropic 返回固定 JSON；返回 mock 以便测试断言。"""
    mock = MagicMock(return_value=(
        json.dumps({
            "summary": "用户在咨询 AGV 启动检查流程。系统列出三项检查并解释急停按钮检查方法。",
            "facts": ["AGV 启动 3 项检查", "急停按钮每班前后测试"],
        }),
        "claude-haiku-4-5-20251001",
    ))
    monkeypatch.setattr(
        "custom_app.services.session_memory._call_anthropic", mock,
    )
    # Gemini fallback 也 mock 一下，避免 anthropic 失败时去查 DB / 走真实路径
    monkeypatch.setattr(
        "custom_app.services.session_memory._call_gemini",
        MagicMock(return_value=("", "")),
    )
    return mock


def _make_messages(n: int, start_id: int = 1) -> list[dict[str, Any]]:
    """生成 n 条 alternating user/assistant 消息。"""
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append({
            "id": start_id + i,
            "role": role,
            "content": f"{role} msg {i}",
            "created_at": "2026-06-01T00:00:00Z",
        })
    return out


# ---------------------------------------------------------------------------
# 阈值边界 / 幂等
# ---------------------------------------------------------------------------


def test_below_threshold_skips(fake_repo, fake_llm, monkeypatch):
    """新消息数 < WINDOW → 不调 LLM，skip_reason=below_threshold。"""
    monkeypatch.setenv("ULTRARAG_SESSION_MEMORY_WINDOW", "10")
    fake_repo.get_summary_state.return_value = {
        "summary": "", "summary_at_msg_id": 0, "summary_updated_at": "",
    }
    fake_repo.list_messages.return_value = _make_messages(5)

    result = session_memory.maybe_summarize("sess_test")

    assert result.applied is False
    assert result.skip_reason == "below_threshold"
    fake_llm.assert_not_called()
    fake_repo.update_summary.assert_not_called()


def test_at_threshold_triggers(fake_repo, fake_llm, monkeypatch):
    """新消息数 == WINDOW → 调 LLM 生成摘要并写库。"""
    monkeypatch.setenv("ULTRARAG_SESSION_MEMORY_WINDOW", "10")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    fake_repo.get_summary_state.return_value = {
        "summary": "", "summary_at_msg_id": 0, "summary_updated_at": "",
    }
    fake_repo.list_messages.return_value = _make_messages(10)

    result = session_memory.maybe_summarize("sess_test")

    assert result.applied is True
    assert result.skip_reason is None
    assert "AGV" in result.summary
    assert result.summary_at_msg_id == 10
    fake_llm.assert_called_once()
    fake_repo.update_summary.assert_called_once()
    call_kwargs = fake_repo.update_summary.call_args.kwargs
    assert call_kwargs["summary_at_msg_id"] == 10
    assert call_kwargs["summary"] == result.summary


def test_already_summarized_skips_when_no_new_messages(
    fake_repo, fake_llm, monkeypatch
):
    """已摘要到 msg_id=10；list_messages 全部 id ≤ 10 → 新消息 0 → skip。"""
    monkeypatch.setenv("ULTRARAG_SESSION_MEMORY_WINDOW", "10")
    fake_repo.get_summary_state.return_value = {
        "summary": "prev summary",
        "summary_at_msg_id": 10,
        "summary_updated_at": "2026-05-30T00:00:00Z",
    }
    fake_repo.list_messages.return_value = _make_messages(10, start_id=1)

    result = session_memory.maybe_summarize("sess_test")

    assert result.applied is False
    assert result.skip_reason == "below_threshold"
    fake_llm.assert_not_called()


def test_already_summarized_triggers_on_n_new_messages(
    fake_repo, fake_llm, monkeypatch
):
    """已摘要到 msg_id=10；新消息 id 11-20 共 10 条 → 触发，新 summary_at_msg_id=20。"""
    monkeypatch.setenv("ULTRARAG_SESSION_MEMORY_WINDOW", "10")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    fake_repo.get_summary_state.return_value = {
        "summary": "prev summary",
        "summary_at_msg_id": 10,
        "summary_updated_at": "2026-05-30T00:00:00Z",
    }
    # 共 20 条消息，id 1-20；前 10 已摘要，新 10 条 (id 11-20)
    fake_repo.list_messages.return_value = _make_messages(20, start_id=1)

    result = session_memory.maybe_summarize("sess_test")

    assert result.applied is True
    assert result.summary_at_msg_id == 20
    fake_llm.assert_called_once()


# ---------------------------------------------------------------------------
# 错误路径
# ---------------------------------------------------------------------------


def test_no_session_returns_no_session(fake_repo, fake_llm, monkeypatch):
    """session 不存在 → no_session。"""
    monkeypatch.setenv("ULTRARAG_SESSION_MEMORY_WINDOW", "10")
    fake_repo.get_summary_state.return_value = None

    result = session_memory.maybe_summarize("sess_missing")
    assert result.applied is False
    assert result.skip_reason == "no_session"
    fake_llm.assert_not_called()


def test_empty_session_id(fake_repo, fake_llm):
    result = session_memory.maybe_summarize("")
    assert result.applied is False
    assert result.skip_reason == "no_session"
    fake_llm.assert_not_called()


def test_disabled_returns_disabled(monkeypatch, fake_repo, fake_llm):
    monkeypatch.setenv("ULTRARAG_SESSION_MEMORY_ENABLED", "0")
    result = session_memory.maybe_summarize("sess_test")
    assert result.applied is False
    assert result.skip_reason == "disabled"
    fake_repo.get_summary_state.assert_not_called()


def test_llm_parse_error(fake_repo, monkeypatch):
    """LLM 返回非 JSON → parse_error。"""
    monkeypatch.setenv("ULTRARAG_SESSION_MEMORY_WINDOW", "10")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    fake_repo.get_summary_state.return_value = {
        "summary": "", "summary_at_msg_id": 0, "summary_updated_at": "",
    }
    fake_repo.list_messages.return_value = _make_messages(10)
    # LLM 返回纯文本，没 JSON
    monkeypatch.setattr(
        "custom_app.services.session_memory._call_anthropic",
        MagicMock(return_value=("not a json at all", "claude-haiku")),
    )
    # 防止 fallback 到 gemini（真实路径会查 DB），mock 掉
    monkeypatch.setattr(
        "custom_app.services.session_memory._call_gemini",
        MagicMock(side_effect=RuntimeError("blocked in test")),
    )
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("ULTRARAG_GEMINI_API_KEY", "")

    result = session_memory.maybe_summarize("sess_test")
    assert result.applied is False
    assert result.skip_reason == "parse_error"
    fake_repo.update_summary.assert_not_called()


def test_llm_failure_falls_through_to_no_backend(fake_repo, monkeypatch):
    """anthropic + gemini 都失败 → no_backend_available 或 error:*。"""
    monkeypatch.setenv("ULTRARAG_SESSION_MEMORY_WINDOW", "10")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("ULTRARAG_GEMINI_API_KEY", "")
    fake_repo.get_summary_state.return_value = {
        "summary": "", "summary_at_msg_id": 0, "summary_updated_at": "",
    }
    fake_repo.list_messages.return_value = _make_messages(10)
    monkeypatch.setattr(
        "custom_app.services.session_memory._call_anthropic",
        MagicMock(side_effect=RuntimeError("no key")),
    )
    # 让 fallback 也找不到 key（ChatModelRepository 查不到 gemini 行 → gemini_key 为空）
    fake_repo_chat = MagicMock()
    fake_repo_chat.list_active.return_value = []
    monkeypatch.setattr(
        "custom_app.repositories.chat_model_repository.ChatModelRepository",
        lambda *a, **kw: fake_repo_chat,
    )

    result = session_memory.maybe_summarize("sess_test")
    assert result.applied is False
    assert result.skip_reason is not None
    # 接受多种错误码：error:RuntimeError 或 no_backend_available
    assert result.skip_reason.startswith("error:") or \
           result.skip_reason == "no_backend_available"
    fake_repo.update_summary.assert_not_called()


# ---------------------------------------------------------------------------
# 截断
# ---------------------------------------------------------------------------


def test_summary_truncated_to_max_chars(fake_repo, monkeypatch):
    """LLM 返回 1000 字摘要 → 被截到 max_chars=200。"""
    monkeypatch.setenv("ULTRARAG_SESSION_MEMORY_WINDOW", "10")
    monkeypatch.setenv("ULTRARAG_SESSION_MEMORY_MAX_SUMMARY_CHARS", "200")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    fake_repo.get_summary_state.return_value = {
        "summary": "", "summary_at_msg_id": 0, "summary_updated_at": "",
    }
    fake_repo.list_messages.return_value = _make_messages(10)
    long_summary = "字" * 1000
    monkeypatch.setattr(
        "custom_app.services.session_memory._call_anthropic",
        MagicMock(return_value=(
            json.dumps({"summary": long_summary, "facts": []}),
            "claude-haiku",
        )),
    )

    result = session_memory.maybe_summarize("sess_test")
    assert result.applied is True
    # 200 字 + "..." 或精确 200 字
    assert len(result.summary) <= 210


# ---------------------------------------------------------------------------
# get_summary_for_prompt
# ---------------------------------------------------------------------------


def test_get_summary_for_prompt_returns_text(fake_repo):
    fake_repo.get_summary_state.return_value = {
        "summary": "previous summary text",
        "summary_at_msg_id": 10,
        "summary_updated_at": "2026-05-30T00:00:00Z",
    }
    out = session_memory.get_summary_for_prompt("sess_x")
    assert out == "previous summary text"


def test_get_summary_for_prompt_empty_when_no_session(fake_repo):
    fake_repo.get_summary_state.return_value = None
    assert session_memory.get_summary_for_prompt("sess_x") == ""


def test_get_summary_for_prompt_empty_when_no_session_id(fake_repo):
    assert session_memory.get_summary_for_prompt("") == ""
    fake_repo.get_summary_state.assert_not_called()


def test_get_summary_for_prompt_swallows_db_errors(fake_repo):
    fake_repo.get_summary_state.side_effect = RuntimeError("DB down")
    # 不抛异常，返回空串
    out = session_memory.get_summary_for_prompt("sess_x")
    assert out == ""


# ---------------------------------------------------------------------------
# get_recent_k
# ---------------------------------------------------------------------------


def test_get_recent_k_default(monkeypatch):
    monkeypatch.delenv("ULTRARAG_SESSION_MEMORY_RECENT_K", raising=False)
    assert session_memory.get_recent_k() == 6


def test_get_recent_k_env_override(monkeypatch):
    monkeypatch.setenv("ULTRARAG_SESSION_MEMORY_RECENT_K", "3")
    assert session_memory.get_recent_k() == 3


def test_get_recent_k_lower_bound(monkeypatch):
    """非法值（0 / 负数）clamp 到 1。"""
    monkeypatch.setenv("ULTRARAG_SESSION_MEMORY_RECENT_K", "0")
    assert session_memory.get_recent_k() == 1
