"""Phase 8.2.2.b —— Reciprocal Rank Fusion 融合 vector + keyword 召回。

参考 WeKnora knowledgebase_search_fusion.go:75。

算法（weighted RRF）：
    1. 每路给一份 ranked list；rank 从 1 开始（顶部最相关）
    2. 同一 chunk 在 list 中的得分 = weight / (k + rank)
    3. 多路得分相加 → 按总分降序排序

权重默认 vector=0.7 / keyword=0.3（WeKnora 默认），可经 env / yaml 调整。
"""
from __future__ import annotations

from typing import Iterable

from custom_app.services.vectorstore.base import Hit


def fuse_with_rrf(
    vector_hits: Iterable[Hit],
    keyword_hits: Iterable[Hit],
    *,
    k: int = 60,
    vector_weight: float = 0.7,
    keyword_weight: float = 0.3,
    top_k: int | None = None,
) -> list[Hit]:
    """合并两路 hits，返回融合后的 Hit 列表（按 RRF 总分降序）。

    Args:
        vector_hits:  向量召回（已按相似度降序）
        keyword_hits: 关键词召回（已按 BM25 分数降序）
        k:            RRF 平滑常数（论文默认 60，权重高时可调）
        vector_weight, keyword_weight: 两路权重，调用方负责合理范围（典型 0..1）
        top_k:        截断长度；None 时返回所有融合命中

    Returns:
        list[Hit]：每个 Hit.score 是 RRF 总分（不再是原始 cos/BM25）

    Notes:
        - 一个 chunk 只在某一路出现时仍可入榜，但分数会比两路都命中的低
        - vector_hits / keyword_hits 内部如果有重复 chunk_id，只采用首次（最高）rank
    """
    vector_ranks: dict[str, int] = {}
    for rank, h in enumerate(vector_hits, start=1):
        vector_ranks.setdefault(h.chunk_id, rank)

    keyword_ranks: dict[str, int] = {}
    for rank, h in enumerate(keyword_hits, start=1):
        keyword_ranks.setdefault(h.chunk_id, rank)

    all_ids = set(vector_ranks) | set(keyword_ranks)
    if not all_ids:
        return []

    fused: list[tuple[str, float]] = []
    for cid in all_ids:
        score = 0.0
        if cid in vector_ranks:
            score += vector_weight / (k + vector_ranks[cid])
        if cid in keyword_ranks:
            score += keyword_weight / (k + keyword_ranks[cid])
        fused.append((cid, score))

    fused.sort(key=lambda x: x[1], reverse=True)
    if top_k is not None:
        fused = fused[: int(top_k)]
    return [Hit(chunk_id=cid, score=score) for cid, score in fused]
