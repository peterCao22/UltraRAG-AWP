"""Phase 12.4 Agent Scratchpad 单元测试。

覆盖：
  1. summarize_tool_result：成功 / LLM 失败 / parse 失败 → 都返回 list
  2. AgentScratchpad.push：正常追加 + FIFO 10 条上限
  3. summarize_and_push：累计 ms / total_calls
  4. render_for_system_prompt：空 / 非空格式
  5. rewrite_old_tool_messages_in_place：保留最近 N=1 / 跨多个 tool / 已改写跳过
  6. env 关闭开关
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_app.services import agent_scratchpad
from custom_app.services.agent_scratchpad import (
    AgentScratchpad,
    PLACEHOLDER_MESSAGE,
    render_for_system_prompt,
    rewrite_old_tool_messages_in_place,
    summarize_and_push,
    summarize_tool_result,
)


# ---------------------------------------------------------------------------
# summarize_tool_result
# ---------------------------------------------------------------------------


def test_summarize_success(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(
        "custom_app.services.agent_scratchpad._call_anthropic",
        MagicMock(return_value=(
            json.dumps({"facts": ["Alarm 16 = Master Link Down"]}),
            "claude-haiku",
        )),
    )
    out = summarize_tool_result(
        tool_name="knowledge_search",
        args={"query": "Alarm 16"},
        result=[{"id": "doc1", "contents": "Alarm 16 details..."}],
    )
    assert out == ["Alarm 16 = Master Link Down"]


def test_summarize_returns_two_facts(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(
        "custom_app.services.agent_scratchpad._call_anthropic",
        MagicMock(return_value=(
            json.dumps({"facts": ["fact A", "fact B"]}),
            "claude-haiku",
        )),
    )
    out = summarize_tool_result(tool_name="x", args={}, result={})
    assert out == ["fact A", "fact B"]


def test_summarize_llm_failure_returns_empty(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(
        "custom_app.services.agent_scratchpad._call_anthropic",
        MagicMock(side_effect=RuntimeError("LLM down")),
    )
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("ULTRARAG_GEMINI_API_KEY", "")
    fake_repo = MagicMock()
    fake_repo.list_active.return_value = []
    monkeypatch.setattr(
        "custom_app.repositories.chat_model_repository.ChatModelRepository",
        lambda *a, **kw: fake_repo,
    )

    out = summarize_tool_result(tool_name="x", args={}, result={})
    assert out == []


def test_summarize_parse_error_returns_empty(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(
        "custom_app.services.agent_scratchpad._call_anthropic",
        MagicMock(return_value=("not a json at all", "claude-haiku")),
    )
    monkeypatch.setattr(
        "custom_app.services.agent_scratchpad._call_gemini",
        MagicMock(side_effect=RuntimeError("blocked")),
    )
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("ULTRARAG_GEMINI_API_KEY", "")

    out = summarize_tool_result(tool_name="x", args={}, result={})
    assert out == []


def test_summarize_disabled_returns_empty(monkeypatch):
    monkeypatch.setenv("ULTRARAG_SCRATCHPAD_ENABLED", "0")
    out = summarize_tool_result(tool_name="x", args={}, result={})
    assert out == []


def test_summarize_empty_tool_name_returns_empty():
    out = summarize_tool_result(tool_name="", args={}, result={})
    assert out == []


# ---------------------------------------------------------------------------
# AgentScratchpad.push / FIFO
# ---------------------------------------------------------------------------


def test_scratchpad_push_appends():
    sp = AgentScratchpad()
    sp.push("fact 1", iteration=1)
    sp.push("fact 2", iteration=2)
    assert sp.facts == ["fact 1", "fact 2"]
    assert sp.iteration_added_at == [1, 2]


def test_scratchpad_fifo_caps_at_10_default(monkeypatch):
    monkeypatch.delenv("ULTRARAG_SCRATCHPAD_MAX_FACTS", raising=False)
    sp = AgentScratchpad()
    for i in range(15):
        sp.push(f"fact {i}", iteration=i)
    assert len(sp.facts) == 10
    # 删的是最早的，留的是最新 10 条
    assert sp.facts[0] == "fact 5"
    assert sp.facts[-1] == "fact 14"
    assert sp.iteration_added_at[0] == 5


def test_scratchpad_fifo_custom_cap(monkeypatch):
    monkeypatch.setenv("ULTRARAG_SCRATCHPAD_MAX_FACTS", "3")
    sp = AgentScratchpad()
    for i in range(5):
        sp.push(f"f{i}", iteration=i)
    assert sp.facts == ["f2", "f3", "f4"]


def test_scratchpad_skips_empty_facts():
    sp = AgentScratchpad()
    sp.push("", iteration=1)
    sp.push("   ", iteration=2)
    sp.push("good", iteration=3)
    assert sp.facts == ["good"]


def test_scratchpad_truncates_long_fact():
    sp = AgentScratchpad()
    long = "字" * 500
    sp.push(long, iteration=1)
    assert len(sp.facts[0]) <= 210


def test_scratchpad_to_meta_shape():
    sp = AgentScratchpad()
    sp.push("f1", iteration=1)
    sp.total_summarize_ms = 1234
    sp.total_calls = 3
    m = sp.to_meta()
    assert m["facts"] == ["f1"]
    assert m["iteration_added_at"] == [1]
    assert m["total_summarize_ms"] == 1234
    assert m["total_calls"] == 3
    assert m["size"] == 1


# ---------------------------------------------------------------------------
# summarize_and_push
# ---------------------------------------------------------------------------


def test_summarize_and_push_records_ms_and_calls(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(
        "custom_app.services.agent_scratchpad._call_anthropic",
        MagicMock(return_value=(
            json.dumps({"facts": ["AGV 启动 3 项检查"]}),
            "claude-haiku",
        )),
    )
    sp = AgentScratchpad()
    facts = summarize_and_push(
        sp, tool_name="knowledge_search",
        args={"q": "x"}, result=[], iteration=1,
    )
    assert facts == ["AGV 启动 3 项检查"]
    assert sp.facts == ["AGV 启动 3 项检查"]
    assert sp.total_calls == 1
    assert sp.total_summarize_ms >= 0


def test_summarize_and_push_swallows_exceptions(monkeypatch):
    """summarize_tool_result 异常应该被 summarize_and_push 吞掉，不抛回主循环。"""
    monkeypatch.setattr(
        "custom_app.services.agent_scratchpad.summarize_tool_result",
        MagicMock(side_effect=RuntimeError("unexpected")),
    )
    sp = AgentScratchpad()
    facts = summarize_and_push(
        sp, tool_name="x", args={}, result={}, iteration=1,
    )
    assert facts == []
    assert sp.total_calls == 1  # 仍然计入 1 次调用尝试
    assert sp.facts == []


# ---------------------------------------------------------------------------
# render_for_system_prompt
# ---------------------------------------------------------------------------


def test_render_empty_returns_empty_string():
    sp = AgentScratchpad()
    assert render_for_system_prompt(sp) == ""


def test_render_lists_facts_numbered():
    sp = AgentScratchpad()
    sp.push("fact A", iteration=1)
    sp.push("fact B", iteration=2)
    out = render_for_system_prompt(sp)
    assert "已知事实摘要" in out
    assert "共 2 条" in out
    assert "1. fact A" in out
    assert "2. fact B" in out


# ---------------------------------------------------------------------------
# rewrite_old_tool_messages_in_place
# ---------------------------------------------------------------------------


def test_rewrite_keeps_latest_one_tool_message(monkeypatch):
    monkeypatch.delenv("ULTRARAG_SCRATCHPAD_KEEP_LATEST_N", raising=False)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "user q"},
        {"role": "assistant", "content": "think 1"},
        {"role": "tool", "name": "t1", "content": [{"id": "c1", "data": "raw1"}]},
        {"role": "assistant", "content": "think 2"},
        {"role": "tool", "name": "t2", "content": [{"id": "c2", "data": "raw2"}]},
        {"role": "assistant", "content": "think 3"},
        {"role": "tool", "name": "t3", "content": [{"id": "c3", "data": "raw3"}]},
    ]
    rewritten = rewrite_old_tool_messages_in_place(messages)
    # 最近 1 个 tool message 保留 raw（最后那条 t3）；前 2 个 tool message 被改写
    assert rewritten == 2
    assert messages[-1]["content"] == [{"id": "c3", "data": "raw3"}]
    assert messages[2]["content"] == {
        "_scratchpad_placeholder": True,
        "_note": PLACEHOLDER_MESSAGE,
    }
    assert messages[4]["content"] == {
        "_scratchpad_placeholder": True,
        "_note": PLACEHOLDER_MESSAGE,
    }


def test_rewrite_skips_already_rewritten():
    messages: list[dict[str, Any]] = [
        {"role": "tool", "name": "t1", "content": {
            "_scratchpad_placeholder": True, "_note": PLACEHOLDER_MESSAGE,
        }},
        {"role": "tool", "name": "t2", "content": "raw2"},  # 这条最近，保留
    ]
    rewritten = rewrite_old_tool_messages_in_place(messages)
    # t1 已是占位，跳过；t2 是最近一条，保留
    assert rewritten == 0


def test_rewrite_keep_zero_rewrites_all(monkeypatch):
    monkeypatch.setenv("ULTRARAG_SCRATCHPAD_KEEP_LATEST_N", "0")
    messages: list[dict[str, Any]] = [
        {"role": "tool", "name": "t1", "content": "raw1"},
        {"role": "tool", "name": "t2", "content": "raw2"},
    ]
    rewritten = rewrite_old_tool_messages_in_place(messages)
    assert rewritten == 2
    assert messages[0]["content"]["_scratchpad_placeholder"] is True
    assert messages[1]["content"]["_scratchpad_placeholder"] is True


def test_rewrite_keep_latest_n_2(monkeypatch):
    monkeypatch.setenv("ULTRARAG_SCRATCHPAD_KEEP_LATEST_N", "2")
    messages: list[dict[str, Any]] = [
        {"role": "tool", "name": "t1", "content": "raw1"},
        {"role": "tool", "name": "t2", "content": "raw2"},
        {"role": "tool", "name": "t3", "content": "raw3"},
    ]
    rewritten = rewrite_old_tool_messages_in_place(messages)
    # 保留最后 2 个；只改写 t1
    assert rewritten == 1
    assert messages[0]["content"]["_scratchpad_placeholder"] is True
    assert messages[1]["content"] == "raw2"
    assert messages[2]["content"] == "raw3"


def test_rewrite_ignores_non_tool_messages():
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
    ]
    rewritten = rewrite_old_tool_messages_in_place(messages)
    assert rewritten == 0
    assert messages[0]["content"] == "q"
    assert messages[1]["content"] == "a"


# ---------------------------------------------------------------------------
# env 开关
# ---------------------------------------------------------------------------


def test_is_enabled_default_true(monkeypatch):
    monkeypatch.delenv("ULTRARAG_SCRATCHPAD_ENABLED", raising=False)
    assert agent_scratchpad.is_enabled() is True


def test_is_enabled_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ULTRARAG_SCRATCHPAD_ENABLED", "0")
    assert agent_scratchpad.is_enabled() is False
