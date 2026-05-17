"""Phase 8.2.2.b RRF 融合测试。"""
from __future__ import annotations

import pytest

from custom_app.services.retrieval.rrf import fuse_with_rrf
from custom_app.services.vectorstore.base import Hit


def _h(cid: str, score: float = 0.0) -> Hit:
    return Hit(chunk_id=cid, score=score)


class TestFuseWithRRF:
    def test_both_hits_intersection_ranks_first(self) -> None:
        """两路都命中且都排第一 → 融合后必在最前。"""
        v = [_h("A"), _h("B")]
        k = [_h("A"), _h("C")]
        out = fuse_with_rrf(v, k, vector_weight=0.5, keyword_weight=0.5)
        assert out[0].chunk_id == "A"
        # B 和 C 都只在一路出现，分数应低于 A
        assert out[0].score > out[1].score

    def test_only_in_one_path_still_appears(self) -> None:
        v = [_h("X")]
        k = [_h("Y")]
        out = fuse_with_rrf(v, k)
        assert {h.chunk_id for h in out} == {"X", "Y"}

    def test_weights_shift_ranking(self) -> None:
        """vector_weight 高 → vector 路顶部 chunk 排在 keyword 路顶部之前。"""
        v = [_h("V_top"), _h("shared")]
        k = [_h("K_top"), _h("shared")]
        out_v = fuse_with_rrf(v, k, vector_weight=0.9, keyword_weight=0.1)
        # shared 在两路都靠前应总分最高
        assert out_v[0].chunk_id == "shared"
        # V_top vs K_top：vector_weight 高 → V_top 应在前
        v_idx = next(i for i, h in enumerate(out_v) if h.chunk_id == "V_top")
        k_idx = next(i for i, h in enumerate(out_v) if h.chunk_id == "K_top")
        assert v_idx < k_idx

        out_k = fuse_with_rrf(v, k, vector_weight=0.1, keyword_weight=0.9)
        v_idx = next(i for i, h in enumerate(out_k) if h.chunk_id == "V_top")
        k_idx = next(i for i, h in enumerate(out_k) if h.chunk_id == "K_top")
        assert k_idx < v_idx

    def test_top_k_truncation(self) -> None:
        v = [_h(f"v{i}") for i in range(10)]
        k = [_h(f"k{i}") for i in range(10)]
        out = fuse_with_rrf(v, k, top_k=5)
        assert len(out) == 5

    def test_empty_both_returns_empty(self) -> None:
        assert fuse_with_rrf([], []) == []

    def test_one_empty_one_full(self) -> None:
        v = [_h("A"), _h("B"), _h("C")]
        out = fuse_with_rrf(v, [])
        assert [h.chunk_id for h in out] == ["A", "B", "C"]

    def test_duplicate_in_one_path_uses_first_rank(self) -> None:
        """同一 chunk 在某一路出现两次 → 用第一次（最高）rank。"""
        v = [_h("A"), _h("B"), _h("A")]  # A 出现在 rank 1 和 3
        k = []
        out = fuse_with_rrf(v, k)
        # A 的分数应基于 rank=1，不应再叠加 rank=3
        # 单路融合时分数 = vector_weight / (k + 1) ≈ 0.7/61
        assert out[0].chunk_id == "A"
        assert out[0].score == pytest.approx(0.7 / 61)

    def test_k_constant_affects_score(self) -> None:
        """k 越大，rank 之间的分数差越小（平滑作用）。"""
        v = [_h("A"), _h("B")]
        k = []
        out_small_k = fuse_with_rrf(v, k, k=1)
        out_big_k = fuse_with_rrf(v, k, k=1000)
        gap_small = out_small_k[0].score - out_small_k[1].score
        gap_big = out_big_k[0].score - out_big_k[1].score
        assert gap_small > gap_big
