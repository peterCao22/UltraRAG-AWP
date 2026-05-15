"""Phase 6.2: chunks_io 工具单测。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_app.utils.chunks_io import (
    append_chunks,
    collect_chunk_ids_for_doc,
    doc_id_to_stem,
    remove_doc_from_chunks,
)


def _write_chunks(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


@pytest.fixture
def chunks_path(tmp_path: Path) -> Path:
    return tmp_path / "chunks.jsonl"


class TestRemoveDocFromChunks:
    def test_removes_only_target_doc(self, chunks_path: Path):
        _write_chunks(chunks_path, [
            {"id": "a_1", "doc": "a", "contents": "x"},
            {"id": "a_2", "doc": "a", "contents": "y"},
            {"id": "b_1", "doc": "b", "contents": "z"},
        ])
        removed = remove_doc_from_chunks(chunks_path, "a")
        assert removed == 2
        rows = [json.loads(l) for l in chunks_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(rows) == 1
        assert rows[0]["doc"] == "b"

    def test_no_target_returns_zero(self, chunks_path: Path):
        _write_chunks(chunks_path, [{"id": "a_1", "doc": "a"}])
        assert remove_doc_from_chunks(chunks_path, "nonexistent") == 0
        # 原文件应保留
        rows = chunks_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(rows) == 1

    def test_missing_file_is_noop(self, tmp_path: Path):
        # 不应抛错
        assert remove_doc_from_chunks(tmp_path / "missing.jsonl", "x") == 0

    def test_atomic_overwrite_no_tmp_leftover(self, chunks_path: Path):
        _write_chunks(chunks_path, [
            {"id": "a_1", "doc": "a"},
            {"id": "b_1", "doc": "b"},
        ])
        remove_doc_from_chunks(chunks_path, "a")
        # tmp 不应残留
        tmp = chunks_path.with_suffix(chunks_path.suffix + ".tmp")
        assert not tmp.exists()

    def test_skips_bad_lines(self, chunks_path: Path):
        chunks_path.parent.mkdir(parents=True, exist_ok=True)
        chunks_path.write_text(
            'not json\n'
            '{"id":"a_1","doc":"a"}\n'
            '{"id":"b_1","doc":"b"}\n',
            encoding="utf-8",
        )
        removed = remove_doc_from_chunks(chunks_path, "a")
        assert removed == 1
        rows = [json.loads(l) for l in chunks_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        # 坏行也被清理（不重要——chunks.jsonl 是派生）
        assert len(rows) == 1
        assert rows[0]["doc"] == "b"


class TestAppendChunks:
    def test_appends_to_existing(self, chunks_path: Path):
        _write_chunks(chunks_path, [{"id": "a_1", "doc": "a"}])
        added = append_chunks(chunks_path, [
            {"id": "b_1", "doc": "b"},
            {"id": "b_2", "doc": "b"},
        ])
        assert added == 2
        rows = [json.loads(l) for l in chunks_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(rows) == 3
        assert [r["doc"] for r in rows] == ["a", "b", "b"]

    def test_creates_file_when_missing(self, chunks_path: Path):
        append_chunks(chunks_path, [{"id": "x_1", "doc": "x"}])
        assert chunks_path.exists()

    def test_empty_is_noop(self, chunks_path: Path):
        assert append_chunks(chunks_path, []) == 0


class TestCollectChunkIds:
    def test_returns_ids_of_target_doc(self, chunks_path: Path):
        _write_chunks(chunks_path, [
            {"id": "a_1", "doc": "a"},
            {"id": "b_1", "doc": "b"},
            {"id": "a_2", "doc": "a"},
        ])
        ids = collect_chunk_ids_for_doc(chunks_path, "a")
        assert sorted(ids) == ["a_1", "a_2"]

    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert collect_chunk_ids_for_doc(tmp_path / "missing.jsonl", "x") == []


class TestDocIdToStem:
    def test_strips_kb_prefix_and_extension(self):
        assert doc_id_to_stem("kbX:foo.docx") == "foo"
        assert doc_id_to_stem("kbX:bar baz.md") == "bar baz"

    def test_handles_no_colon(self):
        assert doc_id_to_stem("legacy.pdf") == "legacy"
