"""Phase 8.1.5 —— 评测指标剥离后的回归测试。

覆盖：
- 8 个生成指标的常规 + 边界 case
- 4 个检索指标的常规 + 边界 case
- compute_generation_metrics / compute_retrieval_metrics 聚合
- 0 行 UltraRAG import 检查
"""
from __future__ import annotations

import math

import pytest

from custom_app.services.eval import metrics as M


# ─────────────────────────────────────────────────────────────────────────────
# normalize_text
# ─────────────────────────────────────────────────────────────────────────────


class TestNormalizeText:
    def test_lower_and_punc(self) -> None:
        assert M.normalize_text("The Quick, Brown Fox.") == "quick brown fox"

    def test_chinese_punc_compressed(self) -> None:
        # 全角逗号/句号应被压成空格，便于中文 token-level F1
        out = M.normalize_text("步骤一：检查电池。检查急停按钮，然后启动。")
        assert "，" not in out and "。" not in out and "：" not in out

    def test_underscore_to_space(self) -> None:
        assert M.normalize_text("step_one") == "step one"

    def test_bool_mapping(self) -> None:
        assert M.normalize_text("True") == "yes"
        assert M.normalize_text("False") == "no"


# ─────────────────────────────────────────────────────────────────────────────
# 生成指标
# ─────────────────────────────────────────────────────────────────────────────


class TestAccuracyScore:
    def test_gt_substring_of_pred(self) -> None:
        assert M.accuracy_score(["paris"], "the capital is Paris.") == 1.0

    def test_no_match(self) -> None:
        assert M.accuracy_score(["paris"], "the capital is London") == 0.0

    def test_empty_pred(self) -> None:
        assert M.accuracy_score(["paris"], "") == 0.0


class TestExactMatchScore:
    def test_equal_after_normalize(self) -> None:
        assert M.exact_match_score(["Paris"], "paris.") == 1.0

    def test_partial_no_match(self) -> None:
        assert M.exact_match_score(["paris"], "paris is the capital") == 0.0


class TestCoverEM:
    def test_all_tokens_present(self) -> None:
        # 所有 gold token 都出现在 pred → 1.0
        assert M.cover_exact_match_score(
            ["check battery emergency stop"],
            "first check the battery; then verify the emergency stop button",
        ) == 1.0

    def test_missing_token(self) -> None:
        assert M.cover_exact_match_score(["alpha beta gamma"], "alpha beta delta") == 0.0

    def test_empty_gt(self) -> None:
        assert M.cover_exact_match_score([], "anything") == 0.0


class TestF1Score:
    def test_full_overlap(self) -> None:
        assert M.f1_score(["alpha beta"], "alpha beta") == pytest.approx(1.0)

    def test_partial_overlap(self) -> None:
        # pred 3 tokens, gt 2 tokens, common=1 → P=1/3, R=1/2 → F1=2/5=0.4
        assert M.f1_score(["alpha beta"], "alpha gamma delta") == pytest.approx(0.4)

    def test_no_overlap_returns_zero(self) -> None:
        assert M.f1_score(["alpha beta"], "delta epsilon") == 0.0

    def test_empty_pred(self) -> None:
        assert M.f1_score(["alpha"], "") == 0.0

    def test_takes_max_across_gold(self) -> None:
        # 两个 gold，pred 与第二个全等 → 选 1.0
        assert M.f1_score(["completely off", "exact match"], "exact match") == pytest.approx(1.0)


class TestStringEM:
    def test_fraction(self) -> None:
        # 2 个 gt，pred 与其中 1 个匹配 → 0.5
        assert M.string_em_score(["paris", "london"], "paris") == 0.5

    def test_empty_gt(self) -> None:
        assert M.string_em_score([], "anything") == 0.0


class TestRouge:
    def test_rouge_l_full_overlap_or_zero(self) -> None:
        # rouge_score 在 venv 已安装；若未装则函数返回 0.0 不抛错（lazy fallback）
        val = M.rouge_l_score(["alpha beta gamma"], "alpha beta gamma")
        # 装了应是 1.0；未装应是 0.0
        assert val in (0.0, pytest.approx(1.0))

    def test_rouge_does_not_raise_without_package(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 强制走 fallback：临时把 _rouge_unavailable 设为 True
        monkeypatch.setattr(M, "_rouge_scorer", None)
        monkeypatch.setattr(M, "_rouge_unavailable", True)
        assert M.rouge_l_score(["x"], "x") == 0.0
        assert M.rouge1_score(["x"], "x") == 0.0
        assert M.rouge2_score(["x"], "x") == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 检索指标
# ─────────────────────────────────────────────────────────────────────────────


class TestRecallAtK:
    def test_basic(self) -> None:
        # gold = {a, b}, top3 = [a, x, b] → 2/2 = 1.0
        assert M.recall_at_k(["a", "b"], ["a", "x", "b", "c"], 3) == 1.0

    def test_partial(self) -> None:
        # gold = {a, b}, top2 = [a, x] → 1/2 = 0.5
        assert M.recall_at_k(["a", "b"], ["a", "x", "b"], 2) == 0.5

    def test_no_hit(self) -> None:
        assert M.recall_at_k(["a"], ["x", "y", "z"], 3) == 0.0

    def test_empty_gold(self) -> None:
        assert M.recall_at_k([], ["a"], 1) == 0.0


class TestHitAtK:
    def test_hit(self) -> None:
        assert M.hit_at_k(["a", "b"], ["x", "a"], 2) == 1.0

    def test_miss(self) -> None:
        assert M.hit_at_k(["a"], ["x", "y"], 2) == 0.0

    def test_top1_strict(self) -> None:
        # gold 在 rank2，hit@1 = 0
        assert M.hit_at_k(["a"], ["x", "a"], 1) == 0.0


class TestMRR:
    def test_first_position(self) -> None:
        assert M.mrr(["a"], ["a", "b"]) == 1.0

    def test_second_position(self) -> None:
        assert M.mrr(["a"], ["x", "a"]) == 0.5

    def test_no_hit(self) -> None:
        assert M.mrr(["a"], ["x", "y", "z"]) == 0.0


class TestNDCGAtK:
    def test_perfect_ranking(self) -> None:
        # gold = {a, b}, retrieved = [a, b, c, d, e], k=5 → DCG = iDCG → 1.0
        assert M.ndcg_at_k(["a", "b"], ["a", "b", "c", "d", "e"], 5) == pytest.approx(1.0)

    def test_worst_ranking(self) -> None:
        # gold = {a}, retrieved = [x, y, z, a], k=3 → 0
        assert M.ndcg_at_k(["a"], ["x", "y", "z", "a"], 3) == 0.0

    def test_mid_ranking(self) -> None:
        # gold = {a}, retrieved = [x, a], k=2 → DCG = 1/log2(3), iDCG = 1/log2(2)=1
        expected = (1 / math.log2(3)) / 1.0
        assert M.ndcg_at_k(["a"], ["x", "a"], 2) == pytest.approx(expected)


# ─────────────────────────────────────────────────────────────────────────────
# 聚合
# ─────────────────────────────────────────────────────────────────────────────


class TestComputeGenerationMetrics:
    def test_averages_match_per_sample(self) -> None:
        gts = [["paris"], ["london"]]
        preds = ["the capital is Paris.", "Tokyo is in Japan"]
        out = M.compute_generation_metrics(gts, preds, metrics=["acc", "f1"])
        assert "avg_acc" in out and "avg_f1" in out
        # acc: 1.0 + 0.0 = 0.5
        assert out["avg_acc"] == pytest.approx(0.5)

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="length mismatch"):
            M.compute_generation_metrics([["a"]], ["x", "y"])

    def test_unknown_metric_does_not_raise(self) -> None:
        out = M.compute_generation_metrics([["a"]], ["a"], metrics=["acc", "unknown_x"])
        assert "avg_acc" in out and "avg_unknown_x" not in out

    def test_default_metrics_all_registered(self) -> None:
        out = M.compute_generation_metrics([["a"]], ["a"])
        for m in M.GEN_METRIC_REGISTRY:
            assert f"avg_{m}" in out


class TestComputeRetrievalMetrics:
    def test_per_k(self) -> None:
        golds = [["a"], ["b"]]
        retrieved = [["a", "x"], ["x", "b", "y"]]
        out = M.compute_retrieval_metrics(golds, retrieved, ks=(1, 5))
        # recall@1: 1.0 + 0.0 = 0.5
        assert out["recall@1"] == pytest.approx(0.5)
        # recall@5: 1.0 + 1.0 = 1.0
        assert out["recall@5"] == pytest.approx(1.0)
        # mrr: 1/1 + 1/2 = 0.75
        assert out["mrr"] == pytest.approx(0.75)
        assert "ndcg@1" in out and "ndcg@5" in out
        assert "hit@1" in out and "hit@5" in out

    def test_empty_input(self) -> None:
        assert M.compute_retrieval_metrics([], []) == {}

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="length mismatch"):
            M.compute_retrieval_metrics([["a"]], [["a"], ["b"]])


# ─────────────────────────────────────────────────────────────────────────────
# 剥离合规：metrics.py 不能 import UltraRAG
# ─────────────────────────────────────────────────────────────────────────────


def test_no_ultrarag_import() -> None:
    """PLAN §九 验收：剥离后 0 行 UltraRAG import 语句。"""
    import re

    src = (
        __import__("custom_app.services.eval.metrics", fromlist=["__file__"]).__file__
    )
    text = open(src, encoding="utf-8").read()
    # 只检查实际 import / from 语句，docstring 里出现 UltraRAG 字样是允许的
    bad = [
        line
        for line in text.splitlines()
        if re.match(r"\s*(import|from)\s+", line) and "ultrarag" in line.lower()
    ]
    assert not bad, f"metrics.py must not import ultrarag; found: {bad}"
