"""Phase 5.1.6 — Repository 在 Postgres 后端上的集成测试。

策略：用唯一的 test_ 前缀 kb_id / role_id 避免污染生产数据；
测试完成后 hard_delete 清理。需要真实 Postgres 服务（局域网 Docker）。

运行：
    .venv\\Scripts\\python.exe -m pytest tests\\test_repositories_postgres.py \
      -v -m requires_postgres
"""

from __future__ import annotations

import os
import uuid
from typing import Iterator

import pytest


pytestmark = pytest.mark.requires_postgres


# ---------------------------------------------------------------------------
# Fixture：注入 Postgres provider
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def postgres_provider():
    """整个测试会话共用一个 Postgres provider（避免反复建连接池）。"""
    from dotenv import load_dotenv

    load_dotenv()
    if not os.environ.get("ULTRARAG_POSTGRES_URI"):
        pytest.skip("ULTRARAG_POSTGRES_URI not set")

    try:
        from custom_app.repositories.postgres_provider import (
            PostgresConnectionProvider,
            init_postgres_schema,
        )
    except ImportError as e:
        pytest.skip(f"psycopg not installed: {e}")

    provider = PostgresConnectionProvider()
    init_postgres_schema(provider)
    yield provider
    provider.close()


@pytest.fixture
def kb_repo(postgres_provider):
    from custom_app.repositories import KbRepository, set_default_provider

    set_default_provider(postgres_provider)
    yield KbRepository(postgres_provider)
    set_default_provider(None)


@pytest.fixture
def job_repo(postgres_provider):
    from custom_app.repositories import JobRepository, set_default_provider

    set_default_provider(postgres_provider)
    yield JobRepository(postgres_provider)
    set_default_provider(None)


@pytest.fixture
def doc_repo(postgres_provider):
    from custom_app.repositories import DocumentRepository, set_default_provider

    set_default_provider(postgres_provider)
    yield DocumentRepository(postgres_provider)
    set_default_provider(None)


@pytest.fixture
def kg_repo(postgres_provider):
    from custom_app.repositories import KgRepository, set_default_provider

    set_default_provider(postgres_provider)
    yield KgRepository(postgres_provider)
    set_default_provider(None)


@pytest.fixture
def test_kb_id() -> Iterator[str]:
    """唯一 kb_id；测试完成后由测试函数自行清理。"""
    return f"pgtest_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# 双后端互证：和 test_repositories.py 用同样的断言
# ---------------------------------------------------------------------------


def test_kb_crud_on_postgres(kb_repo, test_kb_id):
    """KbRepository 在 Postgres 上：完整 CRUD 闭环。"""
    try:
        # create
        assert not kb_repo.exists(test_kb_id)
        kb_repo.create(
            kb_id=test_kb_id, name="PG Test", description="d",
            tenant_id="default", kb_type="general",
            data_path="/tmp", index_path="/tmp/idx", embedding_path="/tmp/emb",
            created_at="2026-05-11T00:00:00Z",
        )
        assert kb_repo.exists(test_kb_id)

        # get
        kb = kb_repo.get(test_kb_id)
        assert kb is not None
        assert kb["kb_id"] == test_kb_id
        assert kb["type"] == "general"
        assert kb["document_count"] == 0

        # update_basic
        kb_repo.update_basic(
            test_kb_id, name="updated", description="new",
            updated_at="2026-05-11T01:00:00Z",
        )
        kb = kb_repo.get(test_kb_id)
        assert kb["name"] == "updated"

        # archive
        kb_repo.archive(test_kb_id, updated_at="2026-05-11T02:00:00Z")
        assert kb_repo.get(test_kb_id) is None
        assert kb_repo.get(test_kb_id, include_archived=True) is not None
    finally:
        kb_repo.hard_delete(test_kb_id)


def test_job_lifecycle_on_postgres(kb_repo, job_repo, test_kb_id):
    """JobRepository: pending → running → success/failed 全生命周期。"""
    try:
        kb_repo.create(
            kb_id=test_kb_id, name="J", description="",
            tenant_id="default", kb_type="sop_docx",
            data_path="/tmp", index_path="/tmp", embedding_path="/tmp",
            created_at="2026-05-11T00:00:00Z",
        )
        job_id = f"j_{uuid.uuid4().hex[:8]}"
        job_repo.create_ingest_job(
            job_id=job_id, tenant_id="default", kb_id=test_kb_id,
            payload={"force_reindex": True}, created_at="2026-05-11T00:00:00Z",
        )
        # pending
        job = job_repo.get(job_id)
        assert job["status"] == "pending"
        assert not job_repo.has_running(test_kb_id)

        # running
        job_repo.mark_running(job_id, started_at="2026-05-11T00:01:00Z")
        assert job_repo.has_running(test_kb_id)

        # success
        job_repo.mark_success(
            job_id, finished_at="2026-05-11T01:00:00Z",
            result={"chunk_count": 23},
        )
        job = job_repo.get(job_id)
        assert job["status"] == "success"
        assert "chunk_count" in job["result_json"]
    finally:
        kb_repo.hard_delete(test_kb_id)


def test_document_upsert_on_postgres(kb_repo, doc_repo, test_kb_id):
    """DocumentRepository upsert ON CONFLICT 在 Postgres 上工作。"""
    try:
        kb_repo.create(
            kb_id=test_kb_id, name="D", description="",
            tenant_id="default", kb_type="sop_docx",
            data_path="/tmp", index_path="/tmp", embedding_path="/tmp",
            created_at="2026-05-11T00:00:00Z",
        )
        doc_repo.upsert(
            kb_id=test_kb_id, tenant_id="default",
            doc_id=f"{test_kb_id}:a.docx",
            file_name="a.docx", file_type="docx", file_path="/old",
            channel="api", status="pending",
            updated_at="2026-05-11T00:00:00Z",
        )
        # 二次 upsert
        doc_repo.upsert(
            kb_id=test_kb_id, tenant_id="default",
            doc_id=f"{test_kb_id}:a.docx",
            file_name="a.docx", file_type="docx", file_path="/new",
            channel="web", status="uploaded",
            updated_at="2026-05-11T01:00:00Z",
        )
        rows = doc_repo.list_for_kb(test_kb_id, limit=10, offset=0)
        assert len(rows) == 1
        assert rows[0]["file_path"] == "/new"
        assert rows[0]["status"] == "uploaded"
    finally:
        kb_repo.hard_delete(test_kb_id)


def test_kg_returning_id_on_postgres(kb_repo, kg_repo, test_kb_id):
    """KgRepository.insert_entity 必须用 RETURNING id（Postgres 无 lastrowid）。"""
    try:
        kb_repo.create(
            kb_id=test_kb_id, name="K", description="",
            tenant_id="default", kb_type="sop_docx",
            data_path="/tmp", index_path="/tmp", embedding_path="/tmp",
            created_at="2026-05-11T00:00:00Z",
        )
        eid = kg_repo.insert_entity(
            kb_id=test_kb_id, entity_name="测试实体", entity_type="Product",
            description="d", chunk_ids_json='["c1"]',
            created_at="2026-05-11T00:00:00Z",
        )
        assert eid > 0
        ent = kg_repo.find_entity_by_name(test_kb_id, "测试实体")
        assert ent["id"] == eid
    finally:
        kg_repo.delete_all_for_kb(test_kb_id)
        kb_repo.hard_delete(test_kb_id)


def test_postgres_provider_backend_name(postgres_provider):
    assert postgres_provider.backend_name == "postgres"
    assert postgres_provider.placeholder == "%s"


def test_adapt_sql_converts_to_postgres_placeholder():
    """adapt_sql 应正确把 SQLite '?' 转成 Postgres '%s'。"""
    from custom_app.repositories.base import adapt_sql
    from custom_app.repositories.postgres_provider import PostgresConnectionProvider

    # 注意：构造 provider 会触发连接池建立；用 lazy 方式仅取 placeholder 值
    class _Stub:
        placeholder = "%s"
        backend_name = "postgres"

    sql = "SELECT * FROM t WHERE id = ? AND kb_id = ?"
    assert adapt_sql(sql, _Stub()) == "SELECT * FROM t WHERE id = %s AND kb_id = %s"
