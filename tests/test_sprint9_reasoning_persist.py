"""
Sprint 9 TDD 测试：推理元数据持久化

覆盖：
- S9-1: kb_session_messages 表新增 reasoning_json 列
- S9-2: session_store.append_chat_turn 接收 reasoning_for_assistant 并写入
- S9-3: list_messages 返回反序列化的 reasoning 字段
- S9-4: chat.py 在 SSE 流期间累积 thought/tool_call/tool_result，done 时一并落库
- S9-5: GET /api/sessions/<id>/messages 响应包含 reasoning 字段
"""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# 共享：内存 SQLite + monkeypatch
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mem_db(monkeypatch, tmp_path):
    """Phase 5.1.7：临时文件型 SQLite + 默认 sqlite provider。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    from custom_app.repositories import set_default_provider
    set_default_provider(None)
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")

    import custom_app.db as db_module
    db_module.init_db()

    conn = sqlite3.connect(tmp_path / "db" / "app.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()
    set_default_provider(None)


@pytest.fixture
def patched_store(mem_db, monkeypatch):
    """Phase 5.1.7：session_store 不再用 get_conn；走 Repository default provider。"""
    import custom_app.services.session_store as ss
    return ss


# ─────────────────────────────────────────────────────────────────────────────
# S9-1: schema
# ─────────────────────────────────────────────────────────────────────────────

class TestSchema:
    def test_kb_session_messages_has_reasoning_json(self, mem_db):
        cur = mem_db.execute("PRAGMA table_info(kb_session_messages)")
        cols = {row["name"] for row in cur.fetchall()}
        assert "reasoning_json" in cols


# ─────────────────────────────────────────────────────────────────────────────
# S9-2 / S9-3: session_store 持久化层
# ─────────────────────────────────────────────────────────────────────────────

class TestAppendChatTurnReasoning:
    def _seed_session(self, mem_db, sid="sess_x", kb="kb_x"):
        from custom_app.db import now_iso
        ts = now_iso()
        mem_db.execute(
            "INSERT INTO kb_sessions (session_id, kb_id, title, agent_mode, created_at, updated_at) "
            "VALUES (?, ?, '', 'agent', ?, ?)",
            (sid, kb, ts, ts),
        )
        mem_db.commit()  # Phase 5.1.7: 用文件型 SQLite 后必须 commit，Repository 才能读到

    def test_default_reasoning_is_empty_dict(self, patched_store, mem_db):
        self._seed_session(mem_db)
        ok = patched_store.append_chat_turn(
            "sess_x", "kb_x", "user q", "assistant a", agent_mode="quick"
        )
        assert ok is True
        msgs = patched_store.list_messages("sess_x")
        assert len(msgs) == 2
        # 用户消息 reasoning 应为空
        assert msgs[0]["reasoning"] == {}
        # 助手消息 reasoning 应为空（quick 模式没传）
        assert msgs[1]["reasoning"] == {}

    def test_assistant_reasoning_persisted(self, patched_store, mem_db):
        self._seed_session(mem_db)
        reasoning = {
            "iterations": 2,
            "events": [
                {"type": "thought", "content": "我需要搜索"},
                {"type": "tool_call", "tool_name": "knowledge_search",
                 "hint": '搜索知识库："换电"'},
                {"type": "tool_result", "tool_name": "knowledge_search",
                 "summary": "找到 5 个结果", "duration_ms": 80},
            ],
        }
        ok = patched_store.append_chat_turn(
            "sess_x", "kb_x", "换电步骤？", "请见以下步骤...",
            agent_mode="agent", reasoning_for_assistant=reasoning,
        )
        assert ok is True

        msgs = patched_store.list_messages("sess_x")
        # 第二条是 assistant
        assistant_msg = msgs[1]
        assert assistant_msg["role"] == "assistant"
        assert assistant_msg["reasoning"]["iterations"] == 2
        assert len(assistant_msg["reasoning"]["events"]) == 3
        assert assistant_msg["reasoning"]["events"][0]["content"] == "我需要搜索"

    def test_user_message_reasoning_remains_empty(self, patched_store, mem_db):
        """user 消息不应有 reasoning，即使 assistant 有。"""
        self._seed_session(mem_db)
        patched_store.append_chat_turn(
            "sess_x", "kb_x", "q", "a",
            agent_mode="agent", reasoning_for_assistant={"iterations": 1, "events": []},
        )
        msgs = patched_store.list_messages("sess_x")
        assert msgs[0]["role"] == "user"
        assert msgs[0]["reasoning"] == {}

    def test_invalid_reasoning_falls_back_to_empty_dict(self, patched_store, mem_db):
        """非 dict 类型的 reasoning 不应崩溃，落库为 {}。"""
        self._seed_session(mem_db)
        patched_store.append_chat_turn(
            "sess_x", "kb_x", "q", "a",
            agent_mode="agent", reasoning_for_assistant="bad string",
        )
        msgs = patched_store.list_messages("sess_x")
        assert msgs[1]["reasoning"] == {}

    def test_corrupted_db_value_yields_empty_reasoning(self, patched_store, mem_db):
        """DB 中已有损坏 JSON 时 list_messages 不应崩溃。"""
        self._seed_session(mem_db)
        from custom_app.db import now_iso
        mem_db.execute(
            "INSERT INTO kb_session_messages (session_id, role, content, reasoning_json, created_at) "
            "VALUES (?, 'assistant', ?, ?, ?)",
            ("sess_x", "answer", "{not valid json", now_iso()),
        )
        mem_db.commit()  # Phase 5.1.7: Repository 读独立连接，需 commit
        msgs = patched_store.list_messages("sess_x")
        assert msgs[0]["reasoning"] == {}


# ─────────────────────────────────────────────────────────────────────────────
# S9-4: chat.py 累积 reasoning 事件并落库
# ─────────────────────────────────────────────────────────────────────────────

class TestChatStreamPersistsReasoning:
    @pytest.fixture(autouse=True)
    def _patch_faiss(self, monkeypatch):
        import sys
        import types
        if "faiss" not in sys.modules:
            mod = types.ModuleType("faiss")
            mod.IndexFlatIP = MagicMock()
            mod.read_index = MagicMock()
            monkeypatch.setitem(sys.modules, "faiss", mod)

    def test_persist_chat_turn_called_with_reasoning(self, monkeypatch):
        """agent 模式 SSE 流结束时，persist_chat_turn 应收到累积的 reasoning。"""
        import custom_app.api.chat as chat_module

        captured: dict = {}

        def fake_persist(sid, kb, user_text, assistant_text, *, agent_mode="quick", reasoning_for_assistant=None):
            captured["sid"] = sid
            captured["agent_mode"] = agent_mode
            captured["reasoning"] = reasoning_for_assistant

        class FakeAgentRunner:
            enabled_tools = None

            def chat_stream(self, question, *, top_k=None, profile=False, history=None):
                yield {"type": "thought", "content": "我开始搜索"}
                yield {"type": "tool_call", "tool_name": "knowledge_search",
                       "hint": '搜索知识库："换电"'}
                yield {"type": "tool_result", "tool_name": "knowledge_search",
                       "summary": "找到 5 个结果", "duration_ms": 80}
                yield {"type": "chunk", "content": "答案文本"}
                yield {"type": "done", "answer": "答案文本",
                       "meta": {"effective_agent_mode": "agent", "iterations": 1}}

        with patch.object(chat_module, "_get_agent_runner", return_value=FakeAgentRunner()), \
             patch.object(chat_module, "list_messages_for_agent", return_value=[]), \
             patch.object(chat_module, "persist_chat_turn", side_effect=fake_persist), \
             patch("custom_app.services.agent_config_store.get_enabled_tools",
                   return_value=["knowledge_search", "list_knowledge_chunks", "final_answer"]):
            from custom_app.app import create_app
            client = create_app().test_client()
            resp = client.post("/api/chat/stream", json={
                "kb_id": "kb_x",
                "question": "换电？",
                "agent_mode": "agent",
                "session_id": "sess_x",
            })
            _ = resp.data

        assert captured.get("sid") == "sess_x"
        assert captured.get("agent_mode") == "agent"
        reasoning = captured.get("reasoning")
        assert isinstance(reasoning, dict)
        assert reasoning.get("iterations") == 1
        events = reasoning.get("events", [])
        types = [e.get("type") for e in events]
        assert "thought" in types
        assert "tool_call" in types
        assert "tool_result" in types

    def test_quick_mode_persists_with_empty_reasoning(self, monkeypatch):
        """quick 模式仍落库，但 reasoning 为空。"""
        import custom_app.api.chat as chat_module

        captured: dict = {}

        def fake_persist(sid, kb, u, a, *, agent_mode="quick", reasoning_for_assistant=None):
            captured["reasoning"] = reasoning_for_assistant
            captured["agent_mode"] = agent_mode

        class FakeRagRunner:
            def __init__(self, *a, **kw):
                pass

            def init(self):
                pass

            def chat_stream(self, *args, **kwargs):
                yield {"type": "chunk", "content": "ok"}
                yield {"type": "done", "answer": "ok"}

        with patch.object(chat_module, "RagRunner", FakeRagRunner), \
             patch.object(chat_module, "persist_chat_turn", side_effect=fake_persist):
            chat_module._runners.clear()
            from custom_app.app import create_app
            client = create_app().test_client()
            resp = client.post("/api/chat/stream", json={
                "kb_id": "kb_q",
                "question": "q",
                "agent_mode": "quick",
                "session_id": "sess_q",
            })
            _ = resp.data

        assert captured.get("agent_mode") == "quick"
        # quick 模式应传 None 或空 dict（视实现），但不能含 thought 之类事件
        r = captured.get("reasoning")
        assert r is None or r == {} or not r.get("events")


# ─────────────────────────────────────────────────────────────────────────────
# S9-5: REST API 反序列化
# ─────────────────────────────────────────────────────────────────────────────

class TestSessionsApiReasoning:
    @pytest.fixture(autouse=True)
    def _patch_faiss(self, monkeypatch):
        import sys
        import types
        if "faiss" not in sys.modules:
            mod = types.ModuleType("faiss")
            mod.IndexFlatIP = MagicMock()
            mod.read_index = MagicMock()
            monkeypatch.setitem(sys.modules, "faiss", mod)

    def test_messages_endpoint_returns_reasoning(self, mem_db, monkeypatch):
        # Phase 5.1.7：mem_db fixture 已设置好 sqlite provider，无需 patch get_conn
        import custom_app.services.session_store as ss
        from custom_app.db import now_iso
        ts = now_iso()
        mem_db.execute(
            "INSERT INTO kb_sessions (session_id, kb_id, title, agent_mode, created_at, updated_at) "
            "VALUES (?, ?, '', 'agent', ?, ?)",
            ("sess_x", "kb_x", ts, ts),
        )
        mem_db.commit()  # Phase 5.1.7: Repository 用独立连接读，必须 commit
        ss.append_chat_turn(
            "sess_x", "kb_x", "q1", "a1",
            agent_mode="agent",
            reasoning_for_assistant={
                "iterations": 1,
                "events": [{"type": "thought", "content": "hello"}],
            },
        )
        from custom_app.app import create_app
        client = create_app().test_client()
        resp = client.get("/api/sessions/sess_x/messages")
        assert resp.status_code == 200
        body = resp.get_json()
        items = body["data"]["items"]
        assert len(items) == 2
        assistant = [m for m in items if m["role"] == "assistant"][0]
        assert "reasoning" in assistant
        assert assistant["reasoning"]["iterations"] == 1
        assert assistant["reasoning"]["events"][0]["type"] == "thought"
