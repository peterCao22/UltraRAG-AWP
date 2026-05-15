"""Phase 5.1.5 — Repository 层单元测试（SQLite 后端）。

策略：每个测试用临时文件 SQLite，跑 init_db() 建表，然后通过 Repository CRUD。
真实 SQL 执行，确保接口在 SQLite 后端工作正常；为 Phase 5.1.6 切 Postgres 时
通过同一套测试集做"双后端互证"。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

import pytest


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch) -> Iterator[Path]:
    """每个测试使用独立 tmp_path 作为工作目录 + 重置 default provider。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    # 重置 default provider，下次 get_default_provider 会按 env 重新创建
    from custom_app.repositories import set_default_provider

    set_default_provider(None)
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")
    yield tmp_path
    set_default_provider(None)


@pytest.fixture
def init_schema():
    """初始化 SQLite schema（含 Phase 4 type 列迁移）。"""
    from custom_app.db import init_db

    init_db()


@pytest.fixture
def kb_repo(init_schema):
    from custom_app.repositories import KbRepository

    return KbRepository()


@pytest.fixture
def job_repo(init_schema):
    from custom_app.repositories import JobRepository

    return JobRepository()


@pytest.fixture
def doc_repo(init_schema):
    from custom_app.repositories import DocumentRepository

    return DocumentRepository()


@pytest.fixture
def session_repo(init_schema):
    from custom_app.repositories import SessionRepository

    return SessionRepository()


@pytest.fixture
def role_repo(init_schema):
    from custom_app.repositories import RoleRepository

    return RoleRepository()


@pytest.fixture
def agent_cfg_repo(init_schema):
    from custom_app.repositories import AgentConfigRepository

    return AgentConfigRepository()


@pytest.fixture
def kg_repo(init_schema):
    from custom_app.repositories import KgRepository

    return KgRepository()


# ---------------------------------------------------------------------------
# KbRepository
# ---------------------------------------------------------------------------


class TestKbRepository:
    def test_create_and_get(self, kb_repo):
        assert not kb_repo.exists("k1")
        kb_repo.create(
            kb_id="k1",
            name="K1",
            description="desc",
            tenant_id="default",
            kb_type="sop_docx",
            data_path="data/kb/k1",
            index_path="data/kb/k1/index/index.index",
            embedding_path="data/kb/k1/embedding/embedding.npy",
            created_at="2026-05-11T00:00:00Z",
        )
        assert kb_repo.exists("k1")
        kb = kb_repo.get("k1")
        assert kb["kb_id"] == "k1"
        assert kb["type"] == "sop_docx"
        assert kb["status"] == "active"
        assert kb["document_count"] == 0

    def test_get_missing_returns_none(self, kb_repo):
        assert kb_repo.get("nope") is None

    def test_get_archived_filtered_by_default(self, kb_repo):
        kb_repo.create(
            kb_id="k1", name="K1", description="", tenant_id="default",
            kb_type="general", data_path="p", index_path="p", embedding_path="p",
            created_at="2026-05-11T00:00:00Z",
        )
        kb_repo.archive("k1", updated_at="2026-05-11T01:00:00Z")
        assert kb_repo.get("k1") is None  # 默认过滤 archived
        assert kb_repo.get("k1", include_archived=True) is not None

    def test_list_paginated_no_role(self, kb_repo):
        for i in range(5):
            kb_repo.create(
                kb_id=f"k{i}", name=f"K{i}", description="", tenant_id="t1",
                kb_type="sop_docx", data_path="p", index_path="p", embedding_path="p",
                created_at=f"2026-05-11T0{i}:00:00Z",
            )
        rows = kb_repo.list_paginated(role_id=None, include_archived=False, limit=10, offset=0)
        assert len(rows) == 5
        # ORDER BY created_at DESC：最新的在前
        assert rows[0]["kb_id"] == "k4"
        assert rows[-1]["kb_id"] == "k0"

    def test_list_paginated_with_pagination(self, kb_repo):
        for i in range(5):
            kb_repo.create(
                kb_id=f"k{i}", name=f"K{i}", description="", tenant_id="t1",
                kb_type="sop_docx", data_path="p", index_path="p", embedding_path="p",
                created_at=f"2026-05-11T0{i}:00:00Z",
            )
        page1 = kb_repo.list_paginated(role_id=None, include_archived=False, limit=2, offset=0)
        page2 = kb_repo.list_paginated(role_id=None, include_archived=False, limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0]["kb_id"] != page2[0]["kb_id"]

    def test_update_basic(self, kb_repo):
        kb_repo.create(
            kb_id="k1", name="old", description="old desc", tenant_id="t1",
            kb_type="sop_docx", data_path="p", index_path="p", embedding_path="p",
            created_at="2026-05-11T00:00:00Z",
        )
        kb_repo.update_basic("k1", name="new", description="new desc", updated_at="2026-05-11T01:00:00Z")
        kb = kb_repo.get("k1")
        assert kb["name"] == "new"
        assert kb["description"] == "new desc"

    def test_mark_indexed(self, kb_repo):
        kb_repo.create(
            kb_id="k1", name="K1", description="", tenant_id="t1",
            kb_type="sop_docx", data_path="p", index_path="p", embedding_path="p",
            created_at="2026-05-11T00:00:00Z",
        )
        kb_repo.mark_indexed("k1", updated_at="2026-05-11T01:00:00Z")
        kb = kb_repo.get("k1")
        assert kb["last_indexed_at"] == "2026-05-11T01:00:00Z"

    def test_hard_delete(self, kb_repo):
        kb_repo.create(
            kb_id="k1", name="K1", description="", tenant_id="t1",
            kb_type="sop_docx", data_path="p", index_path="p", embedding_path="p",
            created_at="2026-05-11T00:00:00Z",
        )
        kb_repo.hard_delete("k1")
        assert not kb_repo.exists("k1")


# ---------------------------------------------------------------------------
# JobRepository
# ---------------------------------------------------------------------------


class TestJobRepository:
    def _create_kb(self, kb_id="k1"):
        from custom_app.repositories import KbRepository
        KbRepository().create(
            kb_id=kb_id, name=kb_id, description="", tenant_id="t1",
            kb_type="sop_docx", data_path="p", index_path="p", embedding_path="p",
            created_at="2026-05-11T00:00:00Z",
        )

    def test_create_and_get(self, job_repo):
        self._create_kb()
        job_repo.create_ingest_job(
            job_id="j1", tenant_id="t1", kb_id="k1",
            payload={"force_reindex": True}, created_at="2026-05-11T00:00:00Z",
        )
        job = job_repo.get("j1")
        assert job["job_id"] == "j1"
        assert job["kb_id"] == "k1"
        assert job["status"] == "pending"
        assert job["job_type"] == "ingest"
        assert json.loads(job["payload_json"]) == {"force_reindex": True}

    def test_has_running_initially_false(self, job_repo):
        self._create_kb()
        assert job_repo.has_running("k1") is False

    def test_mark_running_then_has_running(self, job_repo):
        self._create_kb()
        job_repo.create_ingest_job(
            job_id="j1", tenant_id="t1", kb_id="k1",
            payload={}, created_at="2026-05-11T00:00:00Z",
        )
        job_repo.mark_running("j1", started_at="2026-05-11T00:01:00Z")
        assert job_repo.has_running("k1") is True
        job = job_repo.get("j1")
        assert job["status"] == "running"
        assert job["started_at"] == "2026-05-11T00:01:00Z"

    def test_mark_success(self, job_repo):
        self._create_kb()
        job_repo.create_ingest_job(
            job_id="j1", tenant_id="t1", kb_id="k1",
            payload={}, created_at="2026-05-11T00:00:00Z",
        )
        result = {"chunk_count": 42, "summary": "ok"}
        job_repo.mark_success("j1", finished_at="2026-05-11T01:00:00Z", result=result)
        job = job_repo.get("j1")
        assert job["status"] == "success"
        assert json.loads(job["result_json"]) == result

    def test_mark_failed(self, job_repo):
        self._create_kb()
        job_repo.create_ingest_job(
            job_id="j1", tenant_id="t1", kb_id="k1",
            payload={}, created_at="2026-05-11T00:00:00Z",
        )
        job_repo.mark_failed("j1", finished_at="2026-05-11T01:00:00Z", error="boom")
        job = job_repo.get("j1")
        assert job["status"] == "failed"
        assert job["last_error"] == "boom"

    def test_list_for_kb_ordering(self, job_repo):
        self._create_kb()
        for i in range(3):
            job_repo.create_ingest_job(
                job_id=f"j{i}", tenant_id="t1", kb_id="k1",
                payload={}, created_at=f"2026-05-11T0{i}:00:00Z",
            )
        rows = job_repo.list_for_kb("k1", limit=10, offset=0)
        assert len(rows) == 3
        assert rows[0]["job_id"] == "j2"  # 最新在前


# ---------------------------------------------------------------------------
# DocumentRepository
# ---------------------------------------------------------------------------


class TestDocumentRepository:
    def _create_kb(self):
        from custom_app.repositories import KbRepository
        KbRepository().create(
            kb_id="k1", name="K1", description="", tenant_id="t1",
            kb_type="sop_docx", data_path="p", index_path="p", embedding_path="p",
            created_at="2026-05-11T00:00:00Z",
        )

    def test_upsert_inserts_new(self, doc_repo):
        self._create_kb()
        doc_repo.upsert(
            kb_id="k1", tenant_id="t1", doc_id="k1:a.docx",
            file_name="a.docx", file_type="docx", file_path="/tmp/a.docx",
            channel="api", status="pending", updated_at="2026-05-11T00:00:00Z",
        )
        rows = doc_repo.list_for_kb("k1", limit=10, offset=0)
        assert len(rows) == 1
        assert rows[0]["doc_id"] == "k1:a.docx"

    def test_upsert_updates_existing(self, doc_repo):
        self._create_kb()
        doc_repo.upsert(
            kb_id="k1", tenant_id="t1", doc_id="k1:a.docx",
            file_name="a.docx", file_type="docx", file_path="/tmp/old",
            channel="api", status="pending", updated_at="2026-05-11T00:00:00Z",
        )
        # 同 (kb_id, doc_id) 再次 upsert：file_path / status 应被更新
        doc_repo.upsert(
            kb_id="k1", tenant_id="t1", doc_id="k1:a.docx",
            file_name="a.docx", file_type="docx", file_path="/tmp/new",
            channel="web", status="uploaded", updated_at="2026-05-11T01:00:00Z",
        )
        rows = doc_repo.list_for_kb("k1", limit=10, offset=0)
        assert len(rows) == 1
        assert rows[0]["file_path"] == "/tmp/new"
        assert rows[0]["status"] == "uploaded"
        assert rows[0]["channel"] == "web"

    def test_mark_all_indexed(self, doc_repo):
        """Phase 6.1：mark_all_indexed 现在写 'completed'（旧 'indexed' 仍兼容读取）。"""
        self._create_kb()
        for i in range(3):
            doc_repo.upsert(
                kb_id="k1", tenant_id="t1", doc_id=f"k1:doc{i}.docx",
                file_name=f"doc{i}.docx", file_type="docx", file_path=f"/p/{i}",
                channel="api", status="pending", updated_at="2026-05-11T00:00:00Z",
            )
        doc_repo.mark_all_indexed("k1", updated_at="2026-05-11T01:00:00Z")
        rows = doc_repo.list_for_kb("k1", limit=10, offset=0)
        assert all(r["status"] == "completed" for r in rows)

    def test_mark_pending_failed_only_affects_in_flight(self, doc_repo):
        """Phase 6.1：扩展到 pending/parsing/embedding/indexing 都受影响，completed 不动。"""
        self._create_kb()
        # 已完成的不应被改
        doc_repo.upsert(
            kb_id="k1", tenant_id="t1", doc_id="k1:a",
            file_name="a", file_type="docx", file_path="/p",
            channel="api", status="completed", updated_at="2026-05-11T00:00:00Z",
        )
        doc_repo.upsert(
            kb_id="k1", tenant_id="t1", doc_id="k1:b",
            file_name="b", file_type="docx", file_path="/p",
            channel="api", status="pending", updated_at="2026-05-11T00:00:00Z",
        )
        doc_repo.upsert(
            kb_id="k1", tenant_id="t1", doc_id="k1:c",
            file_name="c", file_type="docx", file_path="/p",
            channel="api", status="parsing", updated_at="2026-05-11T00:00:00Z",
        )
        doc_repo.mark_pending_failed("k1", error="boom", updated_at="2026-05-11T01:00:00Z")
        rows = {r["doc_id"]: r for r in doc_repo.list_for_kb("k1", limit=10, offset=0)}
        assert rows["k1:a"]["status"] == "completed"  # 不动
        assert rows["k1:b"]["status"] == "failed"
        assert rows["k1:b"]["error_message"] == "boom"
        assert rows["k1:c"]["status"] == "failed"
        assert rows["k1:c"]["error_message"] == "boom"

    def test_delete(self, doc_repo):
        self._create_kb()
        doc_repo.upsert(
            kb_id="k1", tenant_id="t1", doc_id="k1:a",
            file_name="a", file_type="docx", file_path="/p",
            channel="api", status="pending", updated_at="2026-05-11T00:00:00Z",
        )
        assert doc_repo.get("k1", "k1:a") is not None
        doc_repo.delete("k1", "k1:a")
        assert doc_repo.get("k1", "k1:a") is None


# ---------------------------------------------------------------------------
# SessionRepository
# ---------------------------------------------------------------------------


class TestSessionRepository:
    def test_create_get_list(self, session_repo):
        session_repo.create_session(
            session_id="s1", kb_id="k1", title="hi", agent_mode="quick",
            created_at="2026-05-11T00:00:00Z",
        )
        s = session_repo.get_session("s1")
        assert s["session_id"] == "s1"
        assert s["agent_mode"] == "quick"

        rows = session_repo.list_sessions_for_kb("k1")
        assert len(rows) == 1

    def test_append_messages(self, session_repo):
        session_repo.create_session(
            session_id="s1", kb_id="k1", title="t", agent_mode="quick",
            created_at="2026-05-11T00:00:00Z",
        )
        session_repo.append_user_message("s1", content="问题1", created_at="2026-05-11T00:01:00Z")
        session_repo.append_assistant_message(
            "s1", content="答案1", reasoning_json='{"x":1}', created_at="2026-05-11T00:02:00Z",
        )
        msgs = session_repo.list_messages("s1")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["reasoning_json"] == '{"x":1}'

    def test_delete_session_cascades_messages(self, session_repo):
        session_repo.create_session(
            session_id="s1", kb_id="k1", title="t", agent_mode="quick",
            created_at="2026-05-11T00:00:00Z",
        )
        session_repo.append_user_message("s1", content="hi", created_at="2026-05-11T00:01:00Z")
        deleted = session_repo.delete_session("s1")
        assert deleted is True
        assert session_repo.get_session("s1") is None
        assert session_repo.list_messages("s1") == []

    def test_delete_nonexistent_returns_false(self, session_repo):
        assert session_repo.delete_session("nope") is False

    def test_update_title_and_mode(self, session_repo):
        session_repo.create_session(
            session_id="s1", kb_id="k1", title="old", agent_mode="quick",
            created_at="2026-05-11T00:00:00Z",
        )
        session_repo.update_title_and_mode(
            "s1", title="new", agent_mode="agent", updated_at="2026-05-11T01:00:00Z",
        )
        s = session_repo.get_session("s1")
        assert s["title"] == "new"
        assert s["agent_mode"] == "agent"


# ---------------------------------------------------------------------------
# RoleRepository
# ---------------------------------------------------------------------------


class TestRoleRepository:
    def test_create_find_list(self, role_repo):
        role_repo.create(role_id="r1", name="admin", description="X", created_at="2026-05-11T00:00:00Z")
        assert role_repo.exists("r1")
        assert role_repo.find_by_name("admin")["role_id"] == "r1"
        rows = role_repo.list_all()
        assert len(rows) == 1

    def test_upsert_permission_and_list(self, role_repo, kb_repo):
        # 先建 role + kb（permission 关联两者）
        role_repo.create(role_id="r1", name="admin", description="", created_at="2026-05-11T00:00:00Z")
        kb_repo.create(
            kb_id="k1", name="K1", description="", tenant_id="t1",
            kb_type="sop_docx", data_path="p", index_path="p", embedding_path="p",
            created_at="2026-05-11T00:00:00Z",
        )
        role_repo.upsert_permission(
            role_id="r1", kb_id="k1", access_level="read",
            updated_at="2026-05-11T00:00:00Z",
        )
        perms = role_repo.list_permissions("r1")
        assert len(perms) == 1
        assert perms[0]["kb_id"] == "k1"
        assert perms[0]["access_level"] == "read"
        assert perms[0]["kb_name"] == "K1"

        # upsert：升级权限
        role_repo.upsert_permission(
            role_id="r1", kb_id="k1", access_level="write",
            updated_at="2026-05-11T01:00:00Z",
        )
        perms = role_repo.list_permissions("r1")
        assert perms[0]["access_level"] == "write"

    def test_delete_role_cascades_permissions(self, role_repo, kb_repo):
        role_repo.create(role_id="r1", name="admin", description="", created_at="2026-05-11T00:00:00Z")
        kb_repo.create(
            kb_id="k1", name="K1", description="", tenant_id="t1",
            kb_type="sop_docx", data_path="p", index_path="p", embedding_path="p",
            created_at="2026-05-11T00:00:00Z",
        )
        role_repo.upsert_permission(
            role_id="r1", kb_id="k1", access_level="read",
            updated_at="2026-05-11T00:00:00Z",
        )
        role_repo.delete("r1")
        assert not role_repo.exists("r1")
        assert role_repo.list_permissions("r1") == []


# ---------------------------------------------------------------------------
# AgentConfigRepository
# ---------------------------------------------------------------------------


class TestAgentConfigRepository:
    def test_upsert_and_get(self, agent_cfg_repo):
        tools_json = json.dumps(["KnowledgeSearchTool", "KeywordSearchTool"])
        agent_cfg_repo.upsert(
            kb_id="k1", enabled_tools_json=tools_json,
            updated_at="2026-05-11T00:00:00Z",
        )
        got = agent_cfg_repo.get_enabled_tools_json("k1")
        assert got == tools_json

    def test_get_missing_returns_none(self, agent_cfg_repo):
        assert agent_cfg_repo.get_enabled_tools_json("nope") is None

    def test_upsert_replaces(self, agent_cfg_repo):
        agent_cfg_repo.upsert(
            kb_id="k1", enabled_tools_json='["A"]',
            updated_at="2026-05-11T00:00:00Z",
        )
        agent_cfg_repo.upsert(
            kb_id="k1", enabled_tools_json='["B"]',
            updated_at="2026-05-11T01:00:00Z",
        )
        assert agent_cfg_repo.get_enabled_tools_json("k1") == '["B"]'


# ---------------------------------------------------------------------------
# KgRepository
# ---------------------------------------------------------------------------


class TestKgRepository:
    def test_insert_and_find_entity(self, kg_repo):
        eid = kg_repo.insert_entity(
            kb_id="k1", entity_name="电池", entity_type="Product",
            description="x", chunk_ids_json='["c1"]',
            created_at="2026-05-11T00:00:00Z",
        )
        assert eid > 0
        ent = kg_repo.find_entity_by_name("k1", "电池")
        assert ent["id"] == eid
        assert ent["chunk_ids"] == '["c1"]'

    def test_update_entity_chunks(self, kg_repo):
        eid = kg_repo.insert_entity(
            kb_id="k1", entity_name="A", entity_type="Concept", description="",
            chunk_ids_json='["c1"]', created_at="2026-05-11T00:00:00Z",
        )
        kg_repo.update_entity_chunks(eid, chunk_ids_json='["c1", "c2"]')
        ent = kg_repo.find_entity_by_name("k1", "A")
        assert ent["chunk_ids"] == '["c1", "c2"]'

    def test_insert_and_find_relation(self, kg_repo):
        sid = kg_repo.insert_entity(
            kb_id="k1", entity_name="电池", entity_type="P", description="",
            chunk_ids_json="[]", created_at="2026-05-11T00:00:00Z",
        )
        tid = kg_repo.insert_entity(
            kb_id="k1", entity_name="AGV", entity_type="P", description="",
            chunk_ids_json="[]", created_at="2026-05-11T00:00:00Z",
        )
        assert kg_repo.find_relation(
            kb_id="k1", source_id=sid, target_id=tid, relation_type="part_of",
        ) is None
        kg_repo.insert_relation(
            kb_id="k1", source_id=sid, target_id=tid, relation_type="part_of",
            description="电池是 AGV 的一部分", strength=8,
            created_at="2026-05-11T00:00:00Z",
        )
        assert kg_repo.find_relation(
            kb_id="k1", source_id=sid, target_id=tid, relation_type="part_of",
        ) is not None

    def test_delete_all_for_kb(self, kg_repo):
        eid = kg_repo.insert_entity(
            kb_id="k1", entity_name="A", entity_type="P", description="",
            chunk_ids_json="[]", created_at="2026-05-11T00:00:00Z",
        )
        kg_repo.insert_relation(
            kb_id="k1", source_id=eid, target_id=eid, relation_type="self",
            description="", strength=1, created_at="2026-05-11T00:00:00Z",
        )
        rel_n, ent_n = kg_repo.delete_all_for_kb("k1")
        assert rel_n == 1 and ent_n == 1
        assert kg_repo.find_entity_by_name("k1", "A") is None

    def test_count(self, kg_repo):
        sid = kg_repo.insert_entity(
            kb_id="k1", entity_name="A", entity_type="P", description="",
            chunk_ids_json="[]", created_at="2026-05-11T00:00:00Z",
        )
        tid = kg_repo.insert_entity(
            kb_id="k1", entity_name="B", entity_type="P", description="",
            chunk_ids_json="[]", created_at="2026-05-11T00:00:00Z",
        )
        kg_repo.insert_relation(
            kb_id="k1", source_id=sid, target_id=tid, relation_type="r",
            description="", strength=1, created_at="2026-05-11T00:00:00Z",
        )
        stats = kg_repo.count_entities_and_relations("k1")
        assert stats["entity_count"] == 2
        assert stats["relation_count"] == 1

    def test_find_relations_for_entities(self, kg_repo):
        sid = kg_repo.insert_entity(
            kb_id="k1", entity_name="电池", entity_type="P", description="d1",
            chunk_ids_json='["c1"]', created_at="2026-05-11T00:00:00Z",
        )
        tid = kg_repo.insert_entity(
            kb_id="k1", entity_name="AGV", entity_type="P", description="d2",
            chunk_ids_json='["c2"]', created_at="2026-05-11T00:00:00Z",
        )
        kg_repo.insert_relation(
            kb_id="k1", source_id=sid, target_id=tid, relation_type="part_of",
            description="rel", strength=8, created_at="2026-05-11T00:00:00Z",
        )
        rows = kg_repo.find_relations_for_entities("k1", ["电池"])
        # 期望至少 2 行：self（种子电池）+ outgoing（电池→AGV）
        directions = {r["direction"] for r in rows}
        assert "self" in directions
        assert "source" in directions  # outgoing

    def test_find_relations_for_empty_entities(self, kg_repo):
        assert kg_repo.find_relations_for_entities("k1", []) == []


# ---------------------------------------------------------------------------
# 基础设施：ConnectionProvider
# ---------------------------------------------------------------------------


class TestConnectionProvider:
    def test_sqlite_provider_placeholder(self):
        from custom_app.repositories import SqliteConnectionProvider

        provider = SqliteConnectionProvider()
        assert provider.placeholder == "?"
        assert provider.backend_name == "sqlite"

    def test_adapt_sql_passthrough_for_sqlite(self):
        from custom_app.repositories.base import adapt_sql, SqliteConnectionProvider

        sql = "SELECT * FROM t WHERE id = ?"
        assert adapt_sql(sql, SqliteConnectionProvider()) == sql

    def test_get_default_provider_returns_sqlite_by_default(self, monkeypatch):
        from custom_app.repositories import (
            SqliteConnectionProvider,
            get_default_provider,
            set_default_provider,
        )

        set_default_provider(None)
        monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")
        provider = get_default_provider()
        assert isinstance(provider, SqliteConnectionProvider)

    def test_set_default_provider_overrides(self):
        from custom_app.repositories import (
            SqliteConnectionProvider,
            get_default_provider,
            set_default_provider,
        )

        custom = SqliteConnectionProvider()
        set_default_provider(custom)
        assert get_default_provider() is custom
        set_default_provider(None)  # cleanup
