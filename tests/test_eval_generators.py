"""Phase 8.1.2 / 8.1.3 —— 评测候选生成脚本的纯函数测试。"""
from __future__ import annotations

import pytest

from custom_app.scripts.extract_eval_queries import _normalize_query
from custom_app.scripts.generate_eval_queries import (
    _parse_questions,
    _sample_chunks_round_robin,
)


class TestNormalizeQuery:
    def test_collapses_whitespace_and_punc(self) -> None:
        a = _normalize_query("如何 修复 AGV ？")
        b = _normalize_query("如何修复agv?")
        assert a == b

    def test_chinese_punc_dropped(self) -> None:
        assert _normalize_query("AGV 急停了，怎么办！") == "agv急停了怎么办"


class TestParseQuestions:
    def test_clean_json(self) -> None:
        text = (
            '{"questions": ['
            '{"query": "AGV 急停怎么办?", "gold_answer": "复位急停按钮"},'
            '{"query": "PLS 灯亮怎么处理?", "gold_answer": "检查传感器"}'
            "]}"
        )
        qs = _parse_questions(text)
        assert len(qs) == 2
        assert qs[0]["query"] == "AGV 急停怎么办?"
        assert qs[1]["gold_answer"] == "检查传感器"

    def test_json_with_surrounding_text(self) -> None:
        # 模型可能输出 "好的，下面是 JSON: { ... }"
        text = (
            "Sure here you go:\n"
            '{"questions": [{"query": "q1", "gold_answer": "a1"}]}'
        )
        qs = _parse_questions(text)
        assert qs == [{"query": "q1", "gold_answer": "a1"}]

    def test_invalid_json_returns_empty(self) -> None:
        assert _parse_questions("no json at all") == []

    def test_missing_fields_skipped(self) -> None:
        text = (
            '{"questions": ['
            '{"query": ""},'
            '{"query": "ok", "gold_answer": ""},'
            '{"query": "ok2", "gold_answer": "a2"}'
            "]}"
        )
        qs = _parse_questions(text)
        assert qs == [{"query": "ok2", "gold_answer": "a2"}]


class TestSampleChunksRoundRobin:
    def test_covers_each_doc_first(self) -> None:
        # 3 docs，要 3 个 chunk → 每个 doc 各 1
        chunks = [
            {"id": "a_1", "doc": "A", "contents": "x"},
            {"id": "a_2", "doc": "A", "contents": "x"},
            {"id": "b_1", "doc": "B", "contents": "x"},
            {"id": "c_1", "doc": "C", "contents": "x"},
            {"id": "c_2", "doc": "C", "contents": "x"},
        ]
        picked = _sample_chunks_round_robin(chunks, n=3, seed=0)
        assert len({c["doc"] for c in picked}) == 3

    def test_respects_n_when_more_than_available(self) -> None:
        chunks = [{"id": f"a_{i}", "doc": "A", "contents": "x"} for i in range(2)]
        picked = _sample_chunks_round_robin(chunks, n=10, seed=0)
        assert len(picked) == 2

    def test_deterministic_with_seed(self) -> None:
        chunks = [{"id": f"a_{i}", "doc": chr(ord("A") + i % 3), "contents": "x"} for i in range(9)]
        p1 = _sample_chunks_round_robin(chunks, n=5, seed=42)
        p2 = _sample_chunks_round_robin(chunks, n=5, seed=42)
        assert [c["id"] for c in p1] == [c["id"] for c in p2]
