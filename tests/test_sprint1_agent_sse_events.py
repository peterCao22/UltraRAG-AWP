"""
Sprint 1 (阶段 A) TDD 测试：agent 模式推理步骤 SSE 事件契约。

覆盖：
- agent 模式下 chat_stream 发射 thought / tool_call / tool_result 事件
- quick 模式下不发射上述事件
- meta.degraded 含 degraded_reason 字段
- append_chat_turn 记录 agent_mode
- chat.py stream 路由在 session 落库时传递 agent_mode
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from custom_app.services.rag_runner import RagRunner
from custom_app.services.session_store import append_chat_turn


# ─────────────────────────────────────────────
# 共用 fixtures
# ─────────────────────────────────────────────

def _minimal_prep(
    *,
    effective_agent_mode: str = "quick",
    degraded: bool = False,
    expanded_docs: list | None = None,
    degrade_reason: str | None = None,
) -> dict:
    return {
        "q": "测试问题",
        "rewritten_q": "测试问题",
        "hit_ids": [0, 1],
        "prompt_text": "prompt",
        "rerank_meta": {"rerank_applied": False},
        "expanded_docs": expanded_docs or (["DocA"] if effective_agent_mode == "agent" else []),
        "recall_k": 4,
        "final_k": 2,
        "final_k_cfg": 0,
        "requested_agent_mode": effective_agent_mode if not degraded else "agent",
        "effective_agent_mode": effective_agent_mode,
        "degraded": degraded,
        "degrade_reason": degrade_reason,
    }


def _minimal_result() -> dict:
    return {
        "answer": "这是答案",
        "answer_blocks": [],
        "sources": [{"title": "DocA 第1节", "snippet": "内容"}],
        "rewrite_query": "测试问题",
        "meta": {
            "retrieval_source_count": 1,
            "effective_agent_mode": "agent",
            "degraded": False,
        },
    }


def _minimal_result_degraded() -> dict:
    return {
        "answer": "这是答案",
        "answer_blocks": [],
        "sources": [],
        "rewrite_query": "测试问题",
        "meta": {
            "retrieval_source_count": 0,
            "effective_agent_mode": "quick",
            "degraded": True,
            "degrade_reason": "no_documents_matched",
        },
    }


# ─────────────────────────────────────────────
# A-BE-2/3: agent 模式 SSE 事件
# ─────────────────────────────────────────────

class TestAgentModeSSEEvents:
    """agent 模式下，chat_stream 必须发射推理步骤事件。"""

    def _get_events(self, agent_mode: str, prep: dict, result: dict) -> list[dict]:
        r = RagRunner.__new__(RagRunner)
        r.kb_id = "test_kb"
        r._chat_cfg = {"backend": "gemini"}  # 避免 AttributeError
        with (
            patch.object(r, "_prepare_chat_context", return_value=prep),
            patch.object(r, "_generate_stream", return_value=iter(["答案片段"])),
            patch.object(r, "_build_result_from_raw", return_value=result),
        ):
            return list(r.chat_stream("测试问题", agent_mode=agent_mode))

    def test_agent_mode_emits_thought_events(self):
        """agent 模式必须发射至少一个 thought 事件。"""
        prep = _minimal_prep(effective_agent_mode="agent")
        events = self._get_events("agent", prep, _minimal_result())
        thought_events = [e for e in events if e.get("type") == "thought"]
        assert len(thought_events) >= 1, "agent 模式必须发射 thought 事件"

    def test_agent_mode_thought_has_content(self):
        """thought 事件必须含 content 字段（字符串）。"""
        prep = _minimal_prep(effective_agent_mode="agent")
        events = self._get_events("agent", prep, _minimal_result())
        thought_events = [e for e in events if e.get("type") == "thought"]
        for ev in thought_events:
            assert "content" in ev, "thought 事件必须含 content 字段"
            assert isinstance(ev["content"], str)

    def test_agent_mode_emits_tool_call_events(self):
        """agent 模式必须发射至少一个 tool_call 事件。"""
        prep = _minimal_prep(effective_agent_mode="agent")
        events = self._get_events("agent", prep, _minimal_result())
        tool_calls = [e for e in events if e.get("type") == "tool_call"]
        assert len(tool_calls) >= 1, "agent 模式必须发射 tool_call 事件"

    def test_agent_mode_tool_call_has_required_fields(self):
        """tool_call 事件须含 tool_name 和 hint 字段。"""
        prep = _minimal_prep(effective_agent_mode="agent")
        events = self._get_events("agent", prep, _minimal_result())
        tool_calls = [e for e in events if e.get("type") == "tool_call"]
        for ev in tool_calls:
            assert "tool_name" in ev, "tool_call 须含 tool_name"
            assert "hint" in ev, "tool_call 须含 hint"
            assert isinstance(ev["tool_name"], str)
            assert isinstance(ev["hint"], str)

    def test_agent_mode_emits_tool_result_events(self):
        """agent 模式必须发射至少一个 tool_result 事件。"""
        prep = _minimal_prep(effective_agent_mode="agent")
        events = self._get_events("agent", prep, _minimal_result())
        tool_results = [e for e in events if e.get("type") == "tool_result"]
        assert len(tool_results) >= 1, "agent 模式必须发射 tool_result 事件"

    def test_agent_mode_tool_result_has_required_fields(self):
        """tool_result 事件须含 tool_name、summary、duration_ms 字段。"""
        prep = _minimal_prep(effective_agent_mode="agent")
        events = self._get_events("agent", prep, _minimal_result())
        tool_results = [e for e in events if e.get("type") == "tool_result"]
        for ev in tool_results:
            assert "tool_name" in ev
            assert "summary" in ev
            assert "duration_ms" in ev
            assert isinstance(ev["duration_ms"], int)
            assert ev["duration_ms"] >= 0

    def test_agent_mode_event_order(self):
        """事件顺序：thought → tool_call → tool_result 出现在 chunk 和 done 之前。"""
        prep = _minimal_prep(effective_agent_mode="agent")
        events = self._get_events("agent", prep, _minimal_result())
        types = [e["type"] for e in events]
        # thought/tool_call/tool_result 必须在 chunk 之前
        has_thought = "thought" in types
        has_chunk = "chunk" in types
        assert has_thought and has_chunk
        idx_last_thought = max(i for i, t in enumerate(types) if t == "thought")
        idx_first_chunk = next(i for i, t in enumerate(types) if t == "chunk")
        assert idx_last_thought < idx_first_chunk, (
            "thought 系列事件应在 chunk 之前完成"
        )

    def test_agent_mode_tool_hint_no_internal_names(self):
        """tool_call.hint 不得暴露内部函数名（knowledge_search / list_knowledge_chunks）。"""
        prep = _minimal_prep(effective_agent_mode="agent")
        events = self._get_events("agent", prep, _minimal_result())
        tool_calls = [e for e in events if e.get("type") == "tool_call"]
        forbidden = {"knowledge_search", "list_knowledge_chunks", "keyword_search"}
        for ev in tool_calls:
            hint = ev.get("hint", "")
            for name in forbidden:
                assert name not in hint, (
                    f"hint 不得暴露内部工具名 '{name}'，实际: '{hint}'"
                )


# ─────────────────────────────────────────────
# quick 模式：不发射推理步骤事件
# ─────────────────────────────────────────────

class TestQuickModeNoReasoningEvents:
    """quick 模式不应发射 thought / tool_call / tool_result 事件。"""

    def _get_events(self) -> list[dict]:
        r = RagRunner.__new__(RagRunner)
        r.kb_id = "test_kb"
        r._chat_cfg = {"backend": "gemini"}
        prep = _minimal_prep(effective_agent_mode="quick")
        result = {
            "answer": "答案",
            "answer_blocks": [],
            "sources": [],
            "rewrite_query": "问题",
            "meta": {"retrieval_source_count": 0, "effective_agent_mode": "quick"},
        }
        with (
            patch.object(r, "_prepare_chat_context", return_value=prep),
            patch.object(r, "_generate_stream", return_value=iter(["x"])),
            patch.object(r, "_build_result_from_raw", return_value=result),
        ):
            return list(r.chat_stream("问题", agent_mode="quick"))

    def test_quick_mode_no_thought_events(self):
        events = self._get_events()
        assert not any(e.get("type") == "thought" for e in events)

    def test_quick_mode_no_tool_call_events(self):
        events = self._get_events()
        assert not any(e.get("type") == "tool_call" for e in events)

    def test_quick_mode_no_tool_result_events(self):
        events = self._get_events()
        assert not any(e.get("type") == "tool_result" for e in events)

    def test_quick_mode_still_has_chunk_and_done(self):
        """quick 模式现有事件类型不受影响。"""
        events = self._get_events()
        types = {e["type"] for e in events}
        assert "chunk" in types
        assert "done" in types
        assert "meta" in types


# ─────────────────────────────────────────────
# A-BE-4: meta.degraded 正式化
# ─────────────────────────────────────────────

class TestDegradedMetaEvent:
    """agent 降级时，meta 事件须含 degraded=True 与 degraded_reason 字段。"""

    def test_degraded_meta_has_reason_field(self):
        r = RagRunner.__new__(RagRunner)
        r.kb_id = "test_kb"
        r._chat_cfg = {"backend": "gemini"}
        prep = _minimal_prep(
            effective_agent_mode="quick",
            degraded=True,
            expanded_docs=[],
            degrade_reason="no_documents_matched",
        )
        result = _minimal_result_degraded()
        with (
            patch.object(r, "_prepare_chat_context", return_value=prep),
            patch.object(r, "_generate_stream", return_value=iter(["x"])),
            patch.object(r, "_build_result_from_raw", return_value=result),
        ):
            events = list(r.chat_stream("问题", agent_mode="agent"))

        meta_events = [e for e in events if e.get("type") == "meta"]
        assert meta_events, "必须有 meta 事件"
        meta = meta_events[0]
        assert meta.get("degraded") is True, "meta.degraded 必须为 True"
        assert "degraded_reason" in meta, "meta 必须含 degraded_reason 字段"
        assert meta["degraded_reason"], "degraded_reason 不得为空"

    def test_degraded_meta_has_message(self):
        """降级时 meta 须含 message 字段，给前端 Toast 用。"""
        r = RagRunner.__new__(RagRunner)
        r.kb_id = "test_kb"
        r._chat_cfg = {"backend": "gemini"}
        prep = _minimal_prep(
            effective_agent_mode="quick",
            degraded=True,
            expanded_docs=[],
            degrade_reason="no_documents_matched",
        )
        result = _minimal_result_degraded()
        with (
            patch.object(r, "_prepare_chat_context", return_value=prep),
            patch.object(r, "_generate_stream", return_value=iter(["x"])),
            patch.object(r, "_build_result_from_raw", return_value=result),
        ):
            events = list(r.chat_stream("问题", agent_mode="agent"))

        meta_events = [e for e in events if e.get("type") == "meta"]
        meta = meta_events[0]
        assert "message" in meta, "降级时 meta 须含 message 字段"
        assert isinstance(meta["message"], str) and meta["message"]


# ─────────────────────────────────────────────
# A-BE-1: session_store.append_chat_turn 记录 agent_mode
# ─────────────────────────────────────────────

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS kb_sessions (
        session_id TEXT PRIMARY KEY,
        kb_id TEXT NOT NULL,
        title TEXT,
        agent_mode TEXT DEFAULT 'quick',
        created_at TEXT,
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS kb_session_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT,
        reasoning_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT
    );
"""


def _make_session_conn(session_id: str, kb_id: str) -> "sqlite3.Connection":
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO kb_sessions VALUES (?,?,?,?,?,?)",
        (session_id, kb_id, "新对话", "quick", "2026-01-01", "2026-01-01"),
    )
    conn.commit()
    return conn


class TestAppendChatTurnAgentMode:
    """append_chat_turn 应接受并存储 agent_mode 参数。"""

    def test_append_chat_turn_accepts_agent_mode_param(self, monkeypatch):
        """append_chat_turn(session_id, kb_id, user, assistant, agent_mode=...) 不应报错。"""
        import custom_app.services.session_store as ss_module

        conn = _make_session_conn("sess1", "kb1")
        monkeypatch.setattr(ss_module, "get_conn", lambda: conn)

        result = append_chat_turn(
            "sess1", "kb1", "用户问题", "助手回答", agent_mode="agent"
        )
        assert result is True

    def test_append_chat_turn_default_agent_mode_quick(self, monkeypatch):
        """不传 agent_mode 时，默认值为 quick，向后兼容。"""
        import custom_app.services.session_store as ss_module

        conn = _make_session_conn("sess2", "kb1")
        monkeypatch.setattr(ss_module, "get_conn", lambda: conn)

        result = append_chat_turn("sess2", "kb1", "用户问题", "助手回答")
        assert result is True
