"""Phase 6.1: 文档级状态 + 启动恢复单测（SQLite 后端）。

覆盖：
    - update_document_status：状态/error_message/chunk_count/processed_at
    - batch_get_documents：只取请求里的 doc_ids
    - list_documents_with_status：含 summary 派生
    - find_stale_processing + recover_stale_documents 启动恢复
    - DocumentRepository.list_for_kb 对 'done'/'indexed' 旧值做兼容映射
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir(exist_ok=True)
    from custom_app.repositories import set_default_provider

    set_default_provider(None)
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")
    yield tmp_path
    set_default_provider(None)


@pytest.fixture
def doc_repo():
    from custom_app.db import init_db
    from custom_app.repositories import DocumentRepository, KbRepository

    init_db()
    KbRepository().create(
        kb_id="kb1", name="KB1", description="", tenant_id="t1",
        kb_type="sop_docx", data_path="p", index_path="p", embedding_path="p",
        created_at="2026-05-15T00:00:00Z",
    )
    return DocumentRepository()


def _upsert(repo, doc_id: str, status: str, updated_at: str = "2026-05-15T00:00:00Z"):
    repo.upsert(
        kb_id="kb1", tenant_id="t1", doc_id=doc_id,
        file_name=doc_id.split(":", 1)[-1], file_type="docx",
        file_path=f"/tmp/{doc_id}", channel="api",
        status=status, updated_at=updated_at,
    )


class TestUpdateDocumentStatus:
    def test_writes_status_and_clears_error_on_success(self, doc_repo):
        _upsert(doc_repo, "kb1:a.docx", "failed")
        doc_repo.update_document_status(
            "kb1", "kb1:a.docx", status="failed",
            updated_at="2026-05-15T01:00:00Z", error_message="boom",
        )
        # 推进到 parsing：应清空 error_message
        doc_repo.update_document_status(
            "kb1", "kb1:a.docx", status="parsing",
            updated_at="2026-05-15T01:01:00Z",
        )
        row = doc_repo.get("kb1", "kb1:a.docx")
        assert row["status"] == "parsing"
        assert row["error_message"] == ""

    def test_writes_chunk_count_and_processed_at_on_completion(self, doc_repo):
        _upsert(doc_repo, "kb1:a.docx", "indexing")
        doc_repo.update_document_status(
            "kb1", "kb1:a.docx", status="completed",
            updated_at="2026-05-15T01:02:00Z",
            chunk_count=42, processed_at="2026-05-15T01:02:00Z",
        )
        row = doc_repo.get("kb1", "kb1:a.docx")
        assert row["status"] == "completed"
        assert row["chunk_count"] == 42
        assert row["processed_at"] == "2026-05-15T01:02:00Z"

    def test_failed_status_writes_error_message(self, doc_repo):
        _upsert(doc_repo, "kb1:a.docx", "parsing")
        doc_repo.update_document_status(
            "kb1", "kb1:a.docx", status="failed",
            updated_at="2026-05-15T01:03:00Z",
            error_message="kaboom",
        )
        row = doc_repo.get("kb1", "kb1:a.docx")
        assert row["status"] == "failed"
        assert row["error_message"] == "kaboom"

    def test_failed_error_truncates_long_messages(self, doc_repo):
        _upsert(doc_repo, "kb1:a.docx", "parsing")
        long = "x" * 1000
        doc_repo.update_document_status(
            "kb1", "kb1:a.docx", status="failed",
            updated_at="2026-05-15T01:03:00Z", error_message=long,
        )
        row = doc_repo.get("kb1", "kb1:a.docx")
        assert len(row["error_message"]) == 500


class TestBatchGetDocuments:
    def test_returns_only_requested(self, doc_repo):
        for name in ("a", "b", "c"):
            _upsert(doc_repo, f"kb1:{name}.docx", "completed")
        rows = doc_repo.batch_get_documents("kb1", ["kb1:a.docx", "kb1:c.docx"])
        ids = sorted(r["doc_id"] for r in rows)
        assert ids == ["kb1:a.docx", "kb1:c.docx"]

    def test_empty_list_returns_empty(self, doc_repo):
        assert doc_repo.batch_get_documents("kb1", []) == []

    def test_normalizes_legacy_status(self, doc_repo):
        # 旧 'indexed' 值应被读出来时映射成 'completed'
        _upsert(doc_repo, "kb1:legacy.docx", "indexed")
        rows = doc_repo.batch_get_documents("kb1", ["kb1:legacy.docx"])
        assert rows[0]["status"] == "completed"


class TestListDocumentsWithStatus:
    def test_summary_counts_each_status(self, doc_repo):
        _upsert(doc_repo, "kb1:a.docx", "completed")
        _upsert(doc_repo, "kb1:b.docx", "completed")
        _upsert(doc_repo, "kb1:c.docx", "parsing")
        _upsert(doc_repo, "kb1:d.docx", "failed")

        bundle = doc_repo.list_documents_with_status("kb1")
        assert len(bundle["documents"]) == 4
        s = bundle["summary"]
        assert s["completed"] == 2
        assert s["parsing"] == 1
        assert s["failed"] == 1
        assert s["pending"] == 0


class TestFindStaleProcessing:
    def test_threshold_filters_recent_rows(self, doc_repo):
        # 一行 1 小时前 parsing → stale；一行刚刚 parsing → 不算 stale
        _upsert(doc_repo, "kb1:stale.docx", "parsing",
                updated_at="2026-05-15T00:00:00Z")
        _upsert(doc_repo, "kb1:fresh.docx", "parsing",
                updated_at="2026-05-15T10:00:00Z")
        # 已 completed 的不动
        _upsert(doc_repo, "kb1:done.docx", "completed",
                updated_at="2026-05-15T00:00:00Z")
        # 阈值在 stale 之后、fresh 之前
        stale = doc_repo.find_stale_processing(threshold_iso="2026-05-15T05:00:00Z")
        ids = {r["doc_id"] for r in stale}
        assert ids == {"kb1:stale.docx"}


class TestRecoverStaleDocuments:
    def test_marks_stale_rows_failed(self, doc_repo, monkeypatch):
        from custom_app.services import doc_status_recovery

        _upsert(doc_repo, "kb1:stuck.docx", "parsing",
                updated_at="2026-05-15T00:00:00Z")
        _upsert(doc_repo, "kb1:fresh.docx", "parsing",
                updated_at=datetime.now(timezone.utc).isoformat())

        # 把阈值改成"距今 1 分钟"，让 stuck 算 stale 而 fresh 不算
        count = doc_status_recovery.recover_stale_documents(minutes=1)
        assert count == 1

        row = doc_repo.get("kb1", "kb1:stuck.docx")
        assert row["status"] == "failed"
        assert row["error_message"] == doc_status_recovery.STALE_ERROR_MESSAGE

        fresh = doc_repo.get("kb1", "kb1:fresh.docx")
        assert fresh["status"] == "parsing"


class TestLegacyStatusMapping:
    def test_done_maps_to_completed_in_list(self, doc_repo):
        _upsert(doc_repo, "kb1:a.docx", "done")
        rows = doc_repo.list_for_kb("kb1", limit=10, offset=0)
        assert rows[0]["status"] == "completed"
