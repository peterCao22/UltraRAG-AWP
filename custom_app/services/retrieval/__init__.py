"""Phase 8.2.2 检索增强模块（BM25 + RRF 融合）。

设计：与 vectorstore 平行，BM25 是另一条"召回路"；
最终通过 RRF 融合给 reranker 一份合并的候选集。

子模块：
    bm25 —— BM25Store：load(rows) → search(query, top_k) → list[Hit]
    rrf  —— fuse_with_rrf(vector_hits, keyword_hits, ...) → list[Hit]
"""
