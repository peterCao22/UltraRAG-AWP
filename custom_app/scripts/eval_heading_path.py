"""Phase 4.3 — heading_path 嵌入增强的 A/B 验证脚本。

用法：
    .venv/Scripts/python.exe -m custom_app.scripts.eval_heading_path \\
        --kb agv_demo \\
        --queries custom_app/scripts/eval_queries.txt \\
        --top-k 5

输出：
    控制台打印每个 query 的 Top-K 命中（含/不含 heading_path 两组）+ 命中差异统计。
    可选 --json 输出结构化结果到文件。

工作流程：
    1. 加载 chunks.jsonl
    2. 对同一组 chunks 做两次嵌入：
        - baseline：仅 title + contents（Phase 3 行为）
        - enhanced：heading_path > title + contents（Phase 4.3 行为）
    3. 对每个 query 做向量检索，对比 Top-K 集合差异
    4. 报告：新增命中数 / 排名提升 / Jaccard 相似度

注意：
    - 需要 GOOGLE_API_KEY，否则 embed_texts 抛错
    - 每条 chunk 会做 2 次 embedding（baseline + enhanced），消耗 API 配额
    - 建议先在小规模（<50 chunks）库上跑，验证后再扩展
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np


def _load_chunks(jsonl_path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _baseline_text(row: dict[str, Any]) -> str:
    """Phase 3 拼接逻辑：仅 title + contents，无 heading_path。"""
    from custom_app.services.google_embedder import strip_images_footer

    title = row.get("title", "") or ""
    body = strip_images_footer(row.get("contents", ""))
    return f"{title}\n{body}".strip()


def _enhanced_text(row: dict[str, Any]) -> str:
    """Phase 4.3 拼接逻辑：含 heading_path 前缀。"""
    from custom_app.services.google_embedder import compose_doc_embedding_text

    return compose_doc_embedding_text(row)


def _embed_batch(texts: list[str]) -> np.ndarray:
    from custom_app.services.google_embedder import embed_texts

    return embed_texts(texts, task_type="RETRIEVAL_DOCUMENT")


def _embed_query(query: str) -> np.ndarray:
    from custom_app.services.google_embedder import embed_query

    return embed_query(query)


def _topk_indices(
    matrix: np.ndarray, query_vec: np.ndarray, k: int
) -> list[tuple[int, float]]:
    """对 (N, D) 矩阵和 (D,) query 计算余弦相似度，返回 Top-K (idx, score)。"""
    scores = matrix @ query_vec
    order = np.argsort(-scores)[:k]
    return [(int(i), float(scores[i])) for i in order]


def _jaccard(a: list[int], b: list[int]) -> float:
    sa, sb = set(a), set(b)
    union = sa | sb
    if not union:
        return 1.0
    return len(sa & sb) / len(union)


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4.3 heading_path A/B 验证")
    parser.add_argument(
        "--kb", required=True, help="KB id，对应 data/kb/<kb>/corpora/chunks.jsonl"
    )
    parser.add_argument(
        "--queries",
        required=True,
        help="查询文件路径（每行一个 query，空行/# 开头被忽略）",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--json", type=str, default="", help="若提供，结构化结果写到该 JSON 文件"
    )
    parser.add_argument(
        "--max-chunks",
        type=int,
        default=0,
        help="限制最多嵌入多少条 chunk（0 = 全部）；用于快速冒烟",
    )
    args = parser.parse_args()

    chunks_path = Path(f"data/kb/{args.kb}/corpora/chunks.jsonl")
    if not chunks_path.exists():
        print(f"ERROR: chunks file not found: {chunks_path}", file=sys.stderr)
        return 2

    queries_path = Path(args.queries)
    if not queries_path.exists():
        print(f"ERROR: queries file not found: {queries_path}", file=sys.stderr)
        return 2

    queries = [
        line.strip()
        for line in queries_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not queries:
        print("ERROR: no queries loaded", file=sys.stderr)
        return 2

    rows = _load_chunks(chunks_path)
    if args.max_chunks > 0:
        rows = rows[: args.max_chunks]
    print(f"Loaded {len(rows)} chunks from {chunks_path}")
    print(f"Loaded {len(queries)} queries")

    # 嵌入两组文本
    print("\n[1/3] 嵌入 baseline（无 heading_path）...")
    baseline_matrix = _embed_batch([_baseline_text(r) for r in rows])
    print(f"  baseline shape: {baseline_matrix.shape}")

    print("\n[2/3] 嵌入 enhanced（含 heading_path）...")
    enhanced_matrix = _embed_batch([_enhanced_text(r) for r in rows])
    print(f"  enhanced shape: {enhanced_matrix.shape}")

    # 对每个 query 比较 Top-K
    print(f"\n[3/3] 对比 Top-{args.top_k} 命中...")
    results: list[dict[str, Any]] = []
    promoted_chunks = 0
    new_hits = 0
    jaccards: list[float] = []

    for q in queries:
        q_vec = _embed_query(q)
        base_hits = _topk_indices(baseline_matrix, q_vec, args.top_k)
        enh_hits = _topk_indices(enhanced_matrix, q_vec, args.top_k)

        base_ids = [i for i, _ in base_hits]
        enh_ids = [i for i, _ in enh_hits]

        added = [i for i in enh_ids if i not in base_ids]
        new_hits += len(added)

        # 排名提升：在 baseline 中排名 r1，在 enhanced 中排名 r2 < r1
        base_rank = {idx: rank for rank, idx in enumerate(base_ids)}
        enh_rank = {idx: rank for rank, idx in enumerate(enh_ids)}
        promoted = [
            idx for idx in enh_ids if idx in base_rank and enh_rank[idx] < base_rank[idx]
        ]
        promoted_chunks += len(promoted)

        j = _jaccard(base_ids, enh_ids)
        jaccards.append(j)

        results.append(
            {
                "query": q,
                "baseline_top": [
                    {"idx": i, "score": s, "id": rows[i].get("id"), "title": rows[i].get("title")}
                    for i, s in base_hits
                ],
                "enhanced_top": [
                    {"idx": i, "score": s, "id": rows[i].get("id"), "title": rows[i].get("title")}
                    for i, s in enh_hits
                ],
                "new_hits": added,
                "promoted": promoted,
                "jaccard": j,
            }
        )

        # 控制台报告
        print(f"\nQUERY: {q}")
        print("  Baseline:")
        for rank, (i, s) in enumerate(base_hits, 1):
            print(f"    {rank}. [{s:.4f}] {rows[i].get('id', '?')}  {rows[i].get('title', '')[:60]}")
        print("  Enhanced:")
        for rank, (i, s) in enumerate(enh_hits, 1):
            marker = " *" if i in added else ("  ^" if i in promoted else "")
            print(f"    {rank}. [{s:.4f}]{marker} {rows[i].get('id', '?')}  {rows[i].get('title', '')[:60]}")
        print(f"  Jaccard(baseline ∩ enhanced) = {j:.3f}")

    # 总览
    avg_jaccard = sum(jaccards) / len(jaccards) if jaccards else 0.0
    summary = {
        "kb": args.kb,
        "queries_count": len(queries),
        "chunks_count": len(rows),
        "top_k": args.top_k,
        "total_new_hits": new_hits,
        "total_promoted": promoted_chunks,
        "avg_jaccard": avg_jaccard,
    }
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(
        "说明：* = enhanced 新增命中（baseline Top-K 中无）；"
        "^ = enhanced 中排名比 baseline 高；Jaccard 越接近 1 改动越小。"
    )

    if args.json:
        out_path = Path(args.json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n结果已保存: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
