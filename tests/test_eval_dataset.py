"""Phase 8.1.1 —— 评测集 schema + dataset IO 测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_app.services.eval.dataset import (
    iter_eval_items,
    load_eval_dataset,
    write_eval_dataset,
)
from custom_app.services.eval.schema import (
    EvalItem,
    validate_kb_homogeneous,
    validate_unique_ids,
)


def _ok_row(rid: str = "eval_001", kb: str = "agv_demo") -> dict:
    return {
        "id": rid,
        "kb_id": kb,
        "query": "AGV 启动前要做哪些检查？",
        "relevant_chunk_ids": ["agv_demo_step_1", "agv_demo_step_2"],
        "gold_answer": "检查电池电量、急停按钮、传感器",
        "tags": ["step_query"],
        "source": "session",
    }


class TestEvalItemFromDict:
    def test_minimal_valid_row_parses(self) -> None:
        row = _ok_row()
        it = EvalItem.from_dict(row)
        assert it.id == "eval_001"
        assert it.kb_id == "agv_demo"
        assert it.relevant_chunk_ids == ("agv_demo_step_1", "agv_demo_step_2")
        assert it.tags == ("step_query",)
        assert it.source == "session"

    def test_omits_optional_fields(self) -> None:
        row = _ok_row()
        row.pop("tags")
        row.pop("source")
        it = EvalItem.from_dict(row)
        assert it.tags == ()
        assert it.source == "manual"

    @pytest.mark.parametrize(
        "patch,err_substr",
        [
            ({"id": ""}, "id"),
            ({"kb_id": ""}, "kb_id"),
            ({"query": "   "}, "query"),
            ({"query": 123}, "query"),
            ({"relevant_chunk_ids": []}, "relevant_chunk_ids"),
            ({"relevant_chunk_ids": ["", "x"]}, "relevant_chunk_ids"),
            ({"gold_answer": ""}, "gold_answer"),
            ({"tags": "not-a-list"}, "tags"),
            ({"tags": [1, 2]}, "tags"),
            ({"source": "weird"}, "source"),
        ],
    )
    def test_invalid_field_raises(self, patch: dict, err_substr: str) -> None:
        row = _ok_row()
        row.update(patch)
        with pytest.raises(ValueError, match=err_substr):
            EvalItem.from_dict(row)

    def test_missing_required_field_raises(self) -> None:
        row = _ok_row()
        row.pop("query")
        with pytest.raises(ValueError, match="missing required fields"):
            EvalItem.from_dict(row)

    def test_round_trip(self) -> None:
        it = EvalItem.from_dict(_ok_row())
        again = EvalItem.from_dict(it.to_dict())
        assert again == it


class TestValidators:
    def test_unique_ids_no_dups(self) -> None:
        items = [
            EvalItem.from_dict(_ok_row(rid="eval_001")),
            EvalItem.from_dict(_ok_row(rid="eval_002")),
        ]
        assert validate_unique_ids(items) == []

    def test_unique_ids_detects_dups(self) -> None:
        items = [
            EvalItem.from_dict(_ok_row(rid="eval_001")),
            EvalItem.from_dict(_ok_row(rid="eval_001")),
        ]
        assert validate_unique_ids(items) == ["eval_001"]

    def test_kb_homogeneous_single(self) -> None:
        items = [EvalItem.from_dict(_ok_row(rid=f"eval_{i:03d}")) for i in range(3)]
        assert validate_kb_homogeneous(items) == {"agv_demo"}

    def test_kb_homogeneous_mixed(self) -> None:
        items = [
            EvalItem.from_dict(_ok_row(rid="eval_001", kb="agv_demo")),
            EvalItem.from_dict(_ok_row(rid="eval_002", kb="ifs_docs")),
        ]
        assert validate_kb_homogeneous(items) == {"agv_demo", "ifs_docs"}


class TestDatasetIO:
    def test_write_then_load_round_trip(self, tmp_path: Path) -> None:
        items = [
            EvalItem.from_dict(_ok_row(rid="eval_001")),
            EvalItem.from_dict(_ok_row(rid="eval_002")),
        ]
        path = tmp_path / "agv_demo.jsonl"
        n = write_eval_dataset(items, path)
        assert n == 2

        loaded = load_eval_dataset(path)
        assert [x.id for x in loaded] == ["eval_001", "eval_002"]
        assert all(x.kb_id == "agv_demo" for x in loaded)

    def test_load_rejects_duplicate_ids(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_text(
            "\n".join(
                json.dumps(_ok_row(rid="x")) for _ in range(2)
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="duplicate"):
            load_eval_dataset(path)

    def test_load_rejects_multi_kb_file(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        rows = [
            _ok_row(rid="a", kb="agv_demo"),
            _ok_row(rid="b", kb="ifs_docs"),
        ]
        path.write_text(
            "\n".join(json.dumps(r) for r in rows),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="multiple kb_id"):
            load_eval_dataset(path)

    def test_load_rejects_kb_mismatch(self, tmp_path: Path) -> None:
        path = tmp_path / "agv.jsonl"
        write_eval_dataset(
            [EvalItem.from_dict(_ok_row(kb="agv_demo"))],
            path,
        )
        with pytest.raises(ValueError, match="expected kb_id"):
            load_eval_dataset(path, expected_kb_id="ifs_docs")

    def test_iter_reports_line_number_on_bad_json(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_text(
            json.dumps(_ok_row()) + "\n" + "not-json-here\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match=":2 "):
            list(iter_eval_items(path))

    def test_iter_skips_blank_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "ok.jsonl"
        path.write_text(
            "\n" + json.dumps(_ok_row()) + "\n\n",
            encoding="utf-8",
        )
        items = list(iter_eval_items(path))
        assert len(items) == 1

    def test_load_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_eval_dataset(tmp_path / "nope.jsonl")

    def test_load_empty_file_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("\n\n", encoding="utf-8")
        with pytest.raises(ValueError, match="empty"):
            load_eval_dataset(path)
