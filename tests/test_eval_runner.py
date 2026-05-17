"""Phase 8.1.5 EvalRunner 单元测试。

策略：mock 出最简 RagRunner stub（只暴露 _prepare_chat_context / _rows / chat），
驱动 EvalRunner 走完 run() 流程，验证：
    - 检索指标按 chunk_id 正确算
    - with_generation=False 时不调 chat()
    - 分桶：tags 样本数 ≥3 才出现在 per_tag_retrieval
    - 失败样本被收集（top-5 未命中）
    - kb_id 不一致时拒绝
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from custom_app.services.eval.dataset import write_eval_dataset
from custom_app.services.eval.runner import EvalRunner
from custom_app.services.eval.schema import EvalItem


class _StubRunner:
    """最小 RagRunner stub：按 query 返回预设的 hit chunk ids。"""

    def __init__(
        self,
        kb_id: str,
        *,
        query_to_chunk_ids: dict[str, list[str]],
        chat_response: dict[str, Any] | None = None,
    ) -> None:
        self.kb_id = kb_id
        self._query_to_chunk_ids = query_to_chunk_ids
        self._chat_response = chat_response or {"answer": "stub answer"}
        # _rows 用行号映射；从所有 hit chunk_ids 建立联合行表
        all_ids: list[str] = []
        for ids in query_to_chunk_ids.values():
            for c in ids:
                if c not in all_ids:
                    all_ids.append(c)
        self._rows = [{"id": c} for c in all_ids]
        self._id_to_row = {c: i for i, c in enumerate(all_ids)}
        self.chat_calls: list[str] = []

    def init(self) -> None:
        pass

    def _prepare_chat_context(self, question: str, top_k: int | None = None) -> dict[str, Any]:
        ids = self._query_to_chunk_ids.get(question, [])
        hit_ids = [self._id_to_row[c] for c in ids if c in self._id_to_row]
        if top_k is not None:
            hit_ids = hit_ids[: int(top_k)]
        return {"hit_ids": hit_ids}

    def chat(self, question: str, top_k: int | None = None) -> dict[str, Any]:
        self.chat_calls.append(question)
        return self._chat_response


def _items_for_test() -> list[EvalItem]:
    return [
        EvalItem.from_dict(
            {
                "id": "eval_001",
                "kb_id": "agv_demo",
                "query": "q1",
                "relevant_chunk_ids": ["c_a"],
                "gold_answer": "answer 1",
                "tags": ["step_query"],
                "source": "session",
            }
        ),
        EvalItem.from_dict(
            {
                "id": "eval_002",
                "kb_id": "agv_demo",
                "query": "q2",
                "relevant_chunk_ids": ["c_b"],
                "gold_answer": "answer 2",
                "tags": ["step_query"],
                "source": "session",
            }
        ),
        EvalItem.from_dict(
            {
                "id": "eval_003",
                "kb_id": "agv_demo",
                "query": "q3",
                "relevant_chunk_ids": ["c_c"],
                "gold_answer": "answer 3",
                "tags": ["step_query"],
                "source": "session",
            }
        ),
        EvalItem.from_dict(
            {
                "id": "eval_004",
                "kb_id": "agv_demo",
                "query": "q4_miss",
                "relevant_chunk_ids": ["c_missing"],
                "gold_answer": "should miss",
                "tags": ["faq"],
                "source": "session",
            }
        ),
    ]


class TestEvalRunnerRetrievalOnly:
    def test_run_without_generation_does_not_call_chat(self) -> None:
        stub = _StubRunner(
            "agv_demo",
            query_to_chunk_ids={
                "q1": ["c_a", "c_z"],
                "q2": ["c_b"],
                "q3": ["c_c", "c_y", "c_x"],
                "q4_miss": ["c_other"],
            },
        )
        runner = EvalRunner("agv_demo", rag_runner=stub, top_k=5)
        runner.set_items(_items_for_test())
        report = runner.run(with_generation=False)
        assert stub.chat_calls == []
        # Recall@5: hit 3/4 = 0.75
        assert report.retrieval_metrics["recall@5"] == pytest.approx(0.75)
        # MRR: q1 rank=1, q2 rank=1, q3 rank=1, q4 miss=0 → (1+1+1+0)/4 = 0.75
        assert report.retrieval_metrics["mrr"] == pytest.approx(0.75)
        assert report.generation_metrics == {}

    def test_per_tag_only_when_3_plus_samples(self) -> None:
        stub = _StubRunner(
            "agv_demo",
            query_to_chunk_ids={
                "q1": ["c_a"], "q2": ["c_b"], "q3": ["c_c"], "q4_miss": ["c_x"],
            },
        )
        runner = EvalRunner("agv_demo", rag_runner=stub, top_k=5)
        runner.set_items(_items_for_test())
        report = runner.run(with_generation=False)
        # step_query 有 3 条 → 应出现；faq 只有 1 条 → 不应出现
        assert "step_query" in report.per_tag_retrieval
        assert "faq" not in report.per_tag_retrieval

    def test_failures_collected_for_miss(self) -> None:
        stub = _StubRunner(
            "agv_demo",
            query_to_chunk_ids={
                "q1": ["c_a"], "q2": ["c_b"], "q3": ["c_c"],
                "q4_miss": ["c_other_1", "c_other_2"],  # 完全没命中 c_missing
            },
        )
        runner = EvalRunner("agv_demo", rag_runner=stub, top_k=5)
        runner.set_items(_items_for_test())
        report = runner.run(with_generation=False)
        failure_ids = [f["id"] for f in report.failures]
        assert "eval_004" in failure_ids
        assert "eval_001" not in failure_ids  # 命中的不应进失败列表

    def test_metadata_includes_timestamp_and_kb(self) -> None:
        stub = _StubRunner("agv_demo", query_to_chunk_ids={"q1": ["c_a"]})
        runner = EvalRunner("agv_demo", rag_runner=stub, top_k=5)
        runner.set_items(
            [
                EvalItem.from_dict(
                    {
                        "id": "x",
                        "kb_id": "agv_demo",
                        "query": "q1",
                        "relevant_chunk_ids": ["c_a"],
                        "gold_answer": "a",
                    }
                )
            ]
        )
        report = runner.run(with_generation=False)
        assert report.run_metadata["kb_id"] == "agv_demo"
        assert report.run_metadata["with_generation"] is False
        assert "timestamp_utc" in report.run_metadata


class TestEvalRunnerWithGeneration:
    def test_chat_called_and_generation_metrics_computed(self) -> None:
        stub = _StubRunner(
            "agv_demo",
            query_to_chunk_ids={
                "q1": ["c_a"], "q2": ["c_b"], "q3": ["c_c"], "q4_miss": ["c_other"],
            },
            chat_response={"answer": "this contains answer 1 inside"},
        )
        runner = EvalRunner("agv_demo", rag_runner=stub, top_k=5)
        runner.set_items(_items_for_test())
        report = runner.run(with_generation=True)
        assert stub.chat_calls == ["q1", "q2", "q3", "q4_miss"]
        # 所有 4 个样本 chat 都返回同一字符串"this contains answer 1 inside"
        # 仅 q1 的 gold "answer 1" 是 pred 子串 → avg_acc = 1/4
        assert report.generation_metrics["avg_acc"] == pytest.approx(0.25)


class TestEvalRunnerDataset:
    def test_load_dataset_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "agv_demo.jsonl"
        write_eval_dataset(_items_for_test(), path)
        runner = EvalRunner("agv_demo")
        n = runner.load_dataset(path)
        assert n == 4

    def test_load_dataset_rejects_kb_mismatch(self, tmp_path: Path) -> None:
        path = tmp_path / "agv_demo.jsonl"
        write_eval_dataset(_items_for_test(), path)
        runner = EvalRunner("ifs_docs")
        with pytest.raises(ValueError, match="expected kb_id"):
            runner.load_dataset(path)

    def test_run_without_loading_raises(self) -> None:
        runner = EvalRunner("agv_demo")
        with pytest.raises(RuntimeError, match="dataset not loaded"):
            runner.run()

    def test_set_items_rejects_foreign_kb(self) -> None:
        runner = EvalRunner("agv_demo")
        items = _items_for_test() + [
            EvalItem.from_dict(
                {
                    "id": "foreign",
                    "kb_id": "ifs_docs",
                    "query": "q",
                    "relevant_chunk_ids": ["x"],
                    "gold_answer": "a",
                }
            )
        ]
        with pytest.raises(ValueError, match="kb_id"):
            runner.set_items(items)
