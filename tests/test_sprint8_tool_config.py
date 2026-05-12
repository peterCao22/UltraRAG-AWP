"""
Sprint 8 TDD 测试：工具可配置化（per-KB 启用工具列表）

覆盖：
- S8-1: agent_config_store 持久化层（CRUD + 默认值 + 强制项保护）
- S8-2: GET / PUT /api/kb/<kb_id>/agent_config 后端 API
- S8-3: chat.py 在 agent 模式下按 KB 配置传 enabled_tools 给 AgentRunner
- S8-4: SQLite 表结构 kb_agent_configs（init_db 时创建）
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# 辅助：内存 SQLite 库 + monkeypatch get_conn
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mem_db(monkeypatch, tmp_path):
    """Phase 5.1.7：用文件型 SQLite 隔离测试 + 通过 Repository default provider 串通。

    旧版本用 :memory: + monkeypatch get_conn；Repository 层接管后这种方式不工作
    （SqliteConnectionProvider 每次 connect/close 独立 conn，:memory: 不能共享）。
    现在改成临时文件型 SQLite + 切换工作目录。
    """
    # 切到 tmp_path 让 db/app.sqlite 在临时目录
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    # 重置 default provider，下次 get_default_provider 按当前环境重新创建
    from custom_app.repositories import set_default_provider
    set_default_provider(None)
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")

    import custom_app.db as db_module
    db_module.init_db()

    # 给老测试一个可直接执行 SQL 的 conn（不参与 Repository 流程，仅用于 fixture 内部）
    conn = sqlite3.connect(tmp_path / "db" / "app.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()
    set_default_provider(None)


# ─────────────────────────────────────────────────────────────────────────────
# S8-4: SQLite 表 kb_agent_configs
# ─────────────────────────────────────────────────────────────────────────────

class TestSchema:
    """init_db 创建 kb_agent_configs 表。"""

    def test_table_exists(self, mem_db):
        cur = mem_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='kb_agent_configs'"
        )
        assert cur.fetchone() is not None

    def test_table_has_kb_id_and_enabled_tools_json(self, mem_db):
        cur = mem_db.execute("PRAGMA table_info(kb_agent_configs)")
        cols = {row["name"] for row in cur.fetchall()}
        assert "kb_id" in cols
        assert "enabled_tools_json" in cols

    def test_kb_id_is_unique(self, mem_db):
        from custom_app.db import now_iso
        ts = now_iso()
        mem_db.execute(
            "INSERT INTO kb_agent_configs (kb_id, enabled_tools_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("agv_demo", '["a"]', ts, ts),
        )
        with pytest.raises(sqlite3.IntegrityError):
            mem_db.execute(
                "INSERT INTO kb_agent_configs (kb_id, enabled_tools_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                ("agv_demo", '["b"]', ts, ts),
            )


# ─────────────────────────────────────────────────────────────────────────────
# S8-1: agent_config_store 持久化层
# ─────────────────────────────────────────────────────────────────────────────

class TestAgentConfigStore:
    """services/agent_config_store.py: 工具启用列表 CRUD。"""

    def _patch_module_get_conn(self, monkeypatch, mem_db):
        """Phase 5.1.7：mem_db fixture 已切到临时 SQLite + sqlite provider，no-op。"""
        return

    def test_module_imports(self):
        from custom_app.services.agent_config_store import (
            get_enabled_tools, set_enabled_tools, ALL_TOOLS, REQUIRED_TOOLS,
        )
        assert callable(get_enabled_tools)
        assert callable(set_enabled_tools)
        assert isinstance(ALL_TOOLS, (list, tuple, set))
        assert isinstance(REQUIRED_TOOLS, (list, tuple, set))

    def test_required_tools_include_final_answer_and_list_chunks(self):
        from custom_app.services.agent_config_store import REQUIRED_TOOLS
        assert "final_answer" in REQUIRED_TOOLS
        assert "list_knowledge_chunks" in REQUIRED_TOOLS

    def test_get_returns_default_when_unset(self, mem_db, monkeypatch):
        self._patch_module_get_conn(monkeypatch, mem_db)
        from custom_app.services.agent_config_store import get_enabled_tools, ALL_TOOLS
        result = get_enabled_tools("never_configured_kb")
        # 默认应包含全部工具（保持向后兼容）
        assert set(result) == set(ALL_TOOLS)

    def test_set_then_get_roundtrip(self, mem_db, monkeypatch):
        self._patch_module_get_conn(monkeypatch, mem_db)
        from custom_app.services.agent_config_store import (
            set_enabled_tools, get_enabled_tools,
        )
        set_enabled_tools("agv_demo", ["knowledge_search", "list_knowledge_chunks", "final_answer"])
        result = get_enabled_tools("agv_demo")
        assert "knowledge_search" in result
        assert "keyword_search" not in result

    def test_set_always_includes_required_tools(self, mem_db, monkeypatch):
        """即使调用方没列出 final_answer / list_knowledge_chunks，set_enabled_tools 也应自动补上。"""
        self._patch_module_get_conn(monkeypatch, mem_db)
        from custom_app.services.agent_config_store import (
            set_enabled_tools, get_enabled_tools,
        )
        set_enabled_tools("agv_demo", ["knowledge_search"])  # 故意漏掉必填项
        result = get_enabled_tools("agv_demo")
        assert "final_answer" in result
        assert "list_knowledge_chunks" in result

    def test_set_filters_unknown_tools(self, mem_db, monkeypatch):
        """白名单：未知工具名应被过滤（防止注入）。"""
        self._patch_module_get_conn(monkeypatch, mem_db)
        from custom_app.services.agent_config_store import (
            set_enabled_tools, get_enabled_tools,
        )
        set_enabled_tools("agv_demo", ["knowledge_search", "evil_tool", "../../../etc/passwd"])
        result = get_enabled_tools("agv_demo")
        assert "evil_tool" not in result
        assert "knowledge_search" in result

    def test_set_overwrites_existing(self, mem_db, monkeypatch):
        self._patch_module_get_conn(monkeypatch, mem_db)
        from custom_app.services.agent_config_store import (
            set_enabled_tools, get_enabled_tools,
        )
        set_enabled_tools("agv_demo", ["knowledge_search"])
        set_enabled_tools("agv_demo", ["keyword_search"])
        result = get_enabled_tools("agv_demo")
        assert "keyword_search" in result
        assert "knowledge_search" not in result

    def test_empty_list_keeps_required_tools_only(self, mem_db, monkeypatch):
        """传空列表也至少留下必填项。"""
        self._patch_module_get_conn(monkeypatch, mem_db)
        from custom_app.services.agent_config_store import (
            set_enabled_tools, get_enabled_tools, REQUIRED_TOOLS,
        )
        set_enabled_tools("agv_demo", [])
        result = get_enabled_tools("agv_demo")
        assert set(REQUIRED_TOOLS).issubset(set(result))


# ─────────────────────────────────────────────────────────────────────────────
# S8-2: REST API GET / PUT /api/kb/<kb_id>/agent_config
# ─────────────────────────────────────────────────────────────────────────────

class TestAgentConfigApi:
    """GET / PUT /api/kb/<kb_id>/agent_config Flask 路由。"""

    @pytest.fixture(autouse=True)
    def _patch_faiss(self, monkeypatch):
        import sys
        import types
        if "faiss" not in sys.modules:
            mod = types.ModuleType("faiss")
            mod.IndexFlatIP = MagicMock()
            mod.read_index = MagicMock()
            mod.write_index = MagicMock()
            monkeypatch.setitem(sys.modules, "faiss", mod)

    def _client(self, monkeypatch, mem_db):
        # Phase 5.1.7: agent_config_store 不再用 get_conn，走 AgentConfigRepository
        # mem_db fixture 已经准备好临时 SQLite + 默认 sqlite provider
        from custom_app.app import create_app
        return create_app().test_client()

    def test_get_returns_default_for_unconfigured_kb(self, mem_db, monkeypatch):
        client = self._client(monkeypatch, mem_db)
        resp = client.get("/api/kb/agv_demo/agent_config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "data" in data or "enabled_tools" in data
        body = data.get("data", data)
        assert "enabled_tools" in body
        assert isinstance(body["enabled_tools"], list)

    def test_get_includes_all_tools_metadata(self, mem_db, monkeypatch):
        """响应应含 all_tools（含描述）让前端渲染勾选框。"""
        client = self._client(monkeypatch, mem_db)
        resp = client.get("/api/kb/agv_demo/agent_config")
        body = resp.get_json().get("data", resp.get_json())
        assert "all_tools" in body
        # all_tools 是 [{"name": ..., "label": ..., "required": bool}, ...]
        names = [t["name"] for t in body["all_tools"]]
        assert "knowledge_search" in names
        assert "final_answer" in names

    def test_put_updates_enabled_tools(self, mem_db, monkeypatch):
        client = self._client(monkeypatch, mem_db)
        resp = client.put(
            "/api/kb/agv_demo/agent_config",
            json={"enabled_tools": ["knowledge_search", "list_knowledge_chunks", "final_answer"]},
        )
        assert resp.status_code == 200
        # 再 GET 验证持久化
        get_resp = client.get("/api/kb/agv_demo/agent_config")
        body = get_resp.get_json().get("data", get_resp.get_json())
        assert "knowledge_search" in body["enabled_tools"]
        assert "keyword_search" not in body["enabled_tools"]

    def test_put_400_on_non_list_body(self, mem_db, monkeypatch):
        client = self._client(monkeypatch, mem_db)
        resp = client.put(
            "/api/kb/agv_demo/agent_config",
            json={"enabled_tools": "not-a-list"},
        )
        assert resp.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# S8-3: chat.py 路由按 KB 配置传 enabled_tools
# ─────────────────────────────────────────────────────────────────────────────

class TestChatRespectsKbConfig:
    """agent 模式 chat_stream 时，按 KB 当前配置覆盖 AgentRunner.enabled_tools。"""

    @pytest.fixture(autouse=True)
    def _patch_faiss(self, monkeypatch):
        import sys
        import types
        if "faiss" not in sys.modules:
            mod = types.ModuleType("faiss")
            mod.IndexFlatIP = MagicMock()
            mod.read_index = MagicMock()
            monkeypatch.setitem(sys.modules, "faiss", mod)

    def test_chat_stream_loads_config_and_sets_enabled_tools(self, monkeypatch):
        """agent 模式调用时，runner.enabled_tools 应被刷新为 store 里的值。"""
        import custom_app.api.chat as chat_module

        captured = {"enabled_tools_at_call": None}

        class FakeAgentRunner:
            def __init__(self):
                self.enabled_tools = None

            def chat_stream(self, question, *, top_k=None, profile=False, history=None):
                captured["enabled_tools_at_call"] = list(self.enabled_tools or [])
                yield {"type": "chunk", "content": "ok"}
                yield {"type": "done", "answer": "ok", "meta": {}}

        fake_runner = FakeAgentRunner()

        with patch.object(chat_module, "_get_agent_runner", return_value=fake_runner), \
             patch.object(chat_module, "list_messages_for_agent", return_value=[]), \
             patch("custom_app.services.agent_config_store.get_enabled_tools",
                   return_value=["knowledge_search", "final_answer", "list_knowledge_chunks"]):
            from custom_app.app import create_app
            client = create_app().test_client()
            resp = client.post("/api/chat/stream", json={
                "kb_id": "agv_demo",
                "question": "测试",
                "agent_mode": "agent",
            })
            _ = resp.data

        # 调用时 enabled_tools 应已是 store 返回的列表
        assert captured["enabled_tools_at_call"] is not None
        assert "knowledge_search" in captured["enabled_tools_at_call"]
        assert "keyword_search" not in captured["enabled_tools_at_call"]
