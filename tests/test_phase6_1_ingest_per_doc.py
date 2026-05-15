"""Phase 6.1: _run_ingest_job 的 stage 广播 + chunk_count 摊派单测。

策略：mock 掉 _parse_stage / _embed_stage / _index_stage / _qdrant_stage /
_kg_stage / _register_documents，让 _run_ingest_job 跑通逻辑而不依赖真实
embedding / FAISS / Qdrant。验证逐文档状态变化与失败传播。
"""

from __future__ import annotations

import json
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
def seeded_kb(tmp_path: Path):
    """建一个带 3 个文档行的 KB（处于 pending）。"""
    from custom_app.db import init_db, new_id, now_iso
    from custom_app.repositories import DocumentRepository, JobRepository, KbRepository

    init_db()
    data_path = str(tmp_path / "kb_root")
    Path(data_path).mkdir(parents=True, exist_ok=True)
    (Path(data_path) / "raw").mkdir(exist_ok=True)
    (Path(data_path) / "corpora").mkdir(exist_ok=True)
    (Path(data_path) / "embedding").mkdir(exist_ok=True)
    (Path(data_path) / "index").mkdir(exist_ok=True)
    KbRepository().create(
        kb_id="kbX", name="KBX", description="", tenant_id="t1",
        kb_type="sop_docx", data_path=data_path,
        index_path=f"{data_path}/index/faiss.idx",
        embedding_path=f"{data_path}/embedding/emb.npy",
        created_at=now_iso(),
    )
    doc_repo = DocumentRepository()
    for name in ("a.docx", "b.docx", "c.docx"):
        doc_repo.upsert(
            kb_id="kbX", tenant_id="t1", doc_id=f"kbX:{name}",
            file_name=name, file_type="docx", file_path=f"{data_path}/raw/{name}",
            channel="api", status="pending", updated_at=now_iso(),
        )
    job_id = new_id("job")
    JobRepository().create_ingest_job(
        job_id=job_id, tenant_id="t1", kb_id="kbX",
        payload={"force_reindex": False}, created_at=now_iso(),
    )
    return {"data_path": data_path, "job_id": job_id}


def _write_chunks(kb_data_path: str, by_doc_stem: dict[str, int]) -> Path:
    """生成 chunks.jsonl：每个 doc_stem 写 N 条 chunk。"""
    out = Path(kb_data_path) / "corpora" / "chunks.jsonl"
    lines = []
    for stem, n in by_doc_stem.items():
        for i in range(n):
            lines.append(json.dumps({"id": f"{stem}_{i}", "title": "", "contents": "x", "doc": stem}))
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def _stub_stages(monkeypatch, kb_data_path: str, by_doc_stem: dict[str, int], *,
                 fail_at: str | None = None) -> None:
    """把 _run_ingest_job 内部 stage 全部 stub 掉。

    fail_at: None / "parse" / "embed" / "index"——指定某 stage 抛错来模拟失败。
    """
    from custom_app.api import kb as kb_mod

    def fake_register(kb, kb_id, raw_dir, chunks_path):
        # 已经 fixture 预 upsert 了，这里什么都不做
        pass

    def fake_scan(kb, raw_dir):
        return [Path("/dev/null/a.docx"), Path("/dev/null/b.docx"), Path("/dev/null/c.docx")]

    def fake_parse(kb, raw_dir, kb_root, chunks_path):
        if fail_at == "parse":
            raise RuntimeError("parse boom")
        _write_chunks(kb_data_path, by_doc_stem)

    def fake_embed(chunks_path, embedding_path):
        if fail_at == "embed":
            raise RuntimeError("embed boom")
        # 写一个最小 npy 占位
        import numpy as np
        emb = np.zeros((sum(by_doc_stem.values()), 4), dtype=np.float32)
        np.save(str(embedding_path), emb)

    def fake_index(chunks_path, embedding_path, index_path):
        if fail_at == "index":
            raise RuntimeError("index boom")
        return sum(by_doc_stem.values())

    def fake_qdrant(*args, **kwargs):
        return 0

    def fake_should_kg(_kb_id):
        return False

    def fake_update_stage(*args, **kwargs):
        pass

    monkeypatch.setattr(kb_mod, "_register_documents", fake_register)
    monkeypatch.setattr(kb_mod, "_scan_raw_files", fake_scan)
    monkeypatch.setattr(kb_mod, "_parse_stage", fake_parse)
    monkeypatch.setattr(kb_mod, "_embed_stage", fake_embed)
    monkeypatch.setattr(kb_mod, "_index_stage", fake_index)
    monkeypatch.setattr(kb_mod, "_qdrant_stage", fake_qdrant)
    monkeypatch.setattr(kb_mod, "_should_extract_kg", fake_should_kg)
    monkeypatch.setattr(kb_mod, "_update_job_stage", fake_update_stage)
    # 防止真去刷 runner 缓存
    monkeypatch.setattr(
        "custom_app.api.chat.invalidate_runner_cache",
        lambda *a, **k: None, raising=False,
    )


class TestRunIngestJobPerDocStatus:
    def test_success_attributes_chunk_count_to_each_doc(self, monkeypatch, seeded_kb):
        from custom_app.api import kb as kb_mod
        from custom_app.repositories import DocumentRepository, KbRepository

        _stub_stages(monkeypatch, seeded_kb["data_path"],
                     by_doc_stem={"a": 3, "b": 5, "c": 2})
        kb = KbRepository().get_basic("kbX")
        res = kb_mod._run_ingest_job(kb, "kbX", seeded_kb["job_id"], False)
        assert res["ok"]

        repo = DocumentRepository()
        bundle = repo.list_documents_with_status("kbX")
        by_id = {d["doc_id"]: d for d in bundle["documents"]}
        for doc_id, expected in [("kbX:a.docx", 3), ("kbX:b.docx", 5), ("kbX:c.docx", 2)]:
            row = by_id[doc_id]
            assert row["status"] == "completed"
            assert row["chunk_count"] == expected
            assert row["processed_at"]  # non-empty timestamp

        assert bundle["summary"]["completed"] == 3
        assert bundle["summary"]["failed"] == 0

    def test_failure_marks_in_flight_docs_failed(self, monkeypatch, seeded_kb):
        from custom_app.api import kb as kb_mod
        from custom_app.repositories import DocumentRepository, KbRepository

        _stub_stages(monkeypatch, seeded_kb["data_path"],
                     by_doc_stem={"a": 1, "b": 1, "c": 1},
                     fail_at="embed")
        kb = KbRepository().get_basic("kbX")
        res = kb_mod._run_ingest_job(kb, "kbX", seeded_kb["job_id"], False)
        assert not res["ok"]

        repo = DocumentRepository()
        bundle = repo.list_documents_with_status("kbX")
        # parse 已经把所有文档推到 parsing；embed 失败时 mark_pending_failed 把所有
        # 还在 pending/parsing/embedding/indexing 的都标 failed
        for d in bundle["documents"]:
            assert d["status"] == "failed", d
            assert d["error_message"]

    def test_stages_broadcast_through_parsing_embedding_indexing(self, monkeypatch, seeded_kb):
        """通过 spy update_document_status 验证 broadcast 顺序覆盖所有文档。"""
        from custom_app.api import kb as kb_mod
        from custom_app.repositories import DocumentRepository, KbRepository

        _stub_stages(monkeypatch, seeded_kb["data_path"],
                     by_doc_stem={"a": 1, "b": 1, "c": 1})

        seen_transitions: list[tuple[str, str]] = []
        real_update = DocumentRepository.update_document_status

        def spy(self, kb_id, doc_id, *, status, updated_at, **kwargs):
            seen_transitions.append((doc_id, status))
            return real_update(self, kb_id, doc_id, status=status,
                               updated_at=updated_at, **kwargs)

        monkeypatch.setattr(DocumentRepository, "update_document_status", spy)

        kb = KbRepository().get_basic("kbX")
        kb_mod._run_ingest_job(kb, "kbX", seeded_kb["job_id"], False)

        per_doc: dict[str, list[str]] = {}
        for doc_id, status in seen_transitions:
            per_doc.setdefault(doc_id, []).append(status)
        for doc_id in ("kbX:a.docx", "kbX:b.docx", "kbX:c.docx"):
            seq = per_doc[doc_id]
            # 三次 stage 广播 + 一次 attribute_chunk_counts 的 completed 覆盖
            assert "parsing" in seq
            assert "embedding" in seq
            assert "indexing" in seq
            assert seq[-1] == "completed"
