"""Phase 5.1.4 — FAISS → Qdrant 迁移 + 一致性验证脚本。

把指定 KB 的 chunks.jsonl + embedding.npy 灌进 Qdrant collection，
然后对同一组 query 比较 FAISS / Qdrant 的 Top-K 命中是否一致。

用法：
    # 1. 迁移（默认 dry-run，看到要写多少 + 不实际写入）
    .venv\\Scripts\\python.exe -m custom_app.scripts.migrate_faiss_to_qdrant \\
        --kb agv_demo --dry-run

    # 2. 真实迁移（先 recreate collection 再 upsert）
    .venv\\Scripts\\python.exe -m custom_app.scripts.migrate_faiss_to_qdrant \\
        --kb agv_demo --recreate

    # 3. 一致性验证（迁移完成后跑，对比 FAISS 和 Qdrant 同 query 的 Top-K）
    .venv\\Scripts\\python.exe -m custom_app.scripts.migrate_faiss_to_qdrant \\
        --kb agv_demo --verify --queries custom_app/scripts/eval_queries_example.txt

退出码：
    0  迁移 / 验证成功
    1  失败（数据缺失、Top-K 不一致等）
    2  使用错误
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

load_dotenv()


def _load_chunks(kb_id: str) -> list[dict]:
    path = Path(f"data/kb/{kb_id}/corpora/chunks.jsonl")
    if not path.exists():
        raise FileNotFoundError(f"chunks file not found: {path}")
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _load_embeddings(kb_id: str) -> np.ndarray:
    path = Path(f"data/kb/{kb_id}/embedding/embedding.npy")
    if not path.exists():
        raise FileNotFoundError(f"embedding file not found: {path}")
    return np.load(str(path))


def _load_faiss_store(kb_id: str, chunks: list[dict]):
    from custom_app.services.vectorstore.faiss_store import FaissVectorStore

    index_path = Path(f"data/kb/{kb_id}/index/index.index")
    if not index_path.exists():
        raise FileNotFoundError(f"faiss index not found: {index_path}")
    chunk_ids = [str(c.get("id", "")) for c in chunks]
    return FaissVectorStore.load(index_path, chunk_ids)


def _build_payloads(chunks: list[dict], kb_id: str) -> list[dict]:
    """构造每个 chunk 的 Qdrant payload。

    保留必要的过滤字段（kb_id / doc / source_type / parser），
    并把完整 chunk dict 存在 chunk_data 中（rag_runner 通过 chunk_id 反查时用）。
    """
    payloads = []
    for c in chunks:
        payloads.append(
            {
                "kb_id": kb_id,
                "doc": c.get("doc", ""),
                "source_type": c.get("source_type", "sop_docx"),
                "parser": c.get("parser", "docx_parser"),
                "chunk_data": c,
            }
        )
    return payloads


# ---------------------------------------------------------------------------
# 子命令：migrate
# ---------------------------------------------------------------------------


def cmd_migrate(args) -> int:
    print(f"=== 迁移 KB={args.kb} : FAISS → Qdrant ===")
    chunks = _load_chunks(args.kb)
    embeddings = _load_embeddings(args.kb)
    print(f"  chunks: {len(chunks)}")
    print(f"  embeddings: {embeddings.shape}")

    if len(chunks) != embeddings.shape[0]:
        print(
            f"ERROR: chunks {len(chunks)} != embeddings rows {embeddings.shape[0]}",
            file=sys.stderr,
        )
        return 1

    chunk_ids = [str(c.get("id", "")) for c in chunks]
    payloads = _build_payloads(chunks, args.kb)

    if args.dry_run:
        print("\n[DRY RUN] 不实际写入 Qdrant。")
        print(f"  将创建 collection: custom_app__{args.kb}")
        print(f"  将 upsert: {len(chunks)} 个 chunk")
        print(f"  embedding 维度: {embeddings.shape[1]}")
        print(f"  payload 字段: {list(payloads[0].keys())}")
        return 0

    from custom_app.services.vectorstore.qdrant_store import QdrantVectorStore

    store = QdrantVectorStore(kb_id=args.kb, embed_dim=embeddings.shape[1])
    print(f"\n[1/3] {'重建' if args.recreate else '确保'} collection {store.collection_name}...")
    store.ensure_collection(recreate=args.recreate)

    print(f"\n[2/3] upsert {len(chunks)} 个 chunk...")
    store.upsert(chunk_ids, embeddings, payloads)
    after = store.size()
    print(f"  Qdrant size after upsert: {after}")

    print("\n[3/3] 简单 sanity check：随机取 3 个 chunk_id，反查 payload...")
    import random

    sample = random.sample(chunk_ids, min(3, len(chunk_ids)))
    for cid in sample:
        data = store.get_chunk_data(cid)
        if data and data.get("chunk_id") == cid:
            print(f"  [OK] {cid} → payload.chunk_id={data['chunk_id']}")
        else:
            print(f"  [FAIL] {cid} → payload missing or mismatch", file=sys.stderr)
            return 1

    print(f"\n[OK] 迁移完成。Qdrant collection={store.collection_name} size={after}")
    return 0


# ---------------------------------------------------------------------------
# 子命令：verify
# ---------------------------------------------------------------------------


def cmd_verify(args) -> int:
    print(f"=== 一致性验证 KB={args.kb} : FAISS vs Qdrant Top-{args.top_k} ===")

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

    chunks = _load_chunks(args.kb)
    print(f"  chunks: {len(chunks)} queries: {len(queries)}")

    from custom_app.services.google_embedder import embed_query
    from custom_app.services.vectorstore.qdrant_store import QdrantVectorStore

    faiss_store = _load_faiss_store(args.kb, chunks)
    qdrant_store = QdrantVectorStore(kb_id=args.kb, embed_dim=faiss_store._index.d)

    # 一致性指标
    perfect_matches = 0
    set_matches = 0  # Top-K 集合相同（不计顺序）
    diffs = []

    for q in queries:
        q_vec = embed_query(q)
        faiss_hits = faiss_store.search(q_vec, top_k=args.top_k)
        qdrant_hits = qdrant_store.search(q_vec, top_k=args.top_k)
        faiss_ids = [h.chunk_id for h in faiss_hits]
        qdrant_ids = [h.chunk_id for h in qdrant_hits]

        if faiss_ids == qdrant_ids:
            perfect_matches += 1
        if set(faiss_ids) == set(qdrant_ids):
            set_matches += 1
        else:
            diffs.append(
                {
                    "query": q,
                    "faiss": faiss_ids,
                    "qdrant": qdrant_ids,
                    "diff_added": [i for i in qdrant_ids if i not in faiss_ids],
                    "diff_missing": [i for i in faiss_ids if i not in qdrant_ids],
                }
            )

    print(f"\n=== 结果 ===")
    print(f"  顺序完全一致: {perfect_matches}/{len(queries)}")
    print(f"  Top-K 集合一致: {set_matches}/{len(queries)}")
    if diffs:
        print(f"\n  集合不一致的 {len(diffs)} 个 query：")
        for d in diffs[:5]:
            print(f"    Q: {d['query']}")
            print(f"      FAISS:  {d['faiss']}")
            print(f"      Qdrant: {d['qdrant']}")
            if d["diff_added"]:
                print(f"      Qdrant 新增: {d['diff_added']}")
            if d["diff_missing"]:
                print(f"      Qdrant 缺失: {d['diff_missing']}")

    # 判定：Top-K 集合一致就算通过（顺序由分数浮点波动决定，可接受小差异）
    if set_matches == len(queries):
        print("\n[OK] 一致性验证通过")
        return 0
    print(f"\n[FAIL] {len(queries) - set_matches}/{len(queries)} 个 query 集合不一致")
    return 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description="Phase 5.1.4 FAISS→Qdrant 迁移工具")
    p.add_argument("--kb", required=True, help="KB id（如 agv_demo / ifs_docs）")
    p.add_argument(
        "--dry-run", action="store_true", help="不实际写 Qdrant，仅打印计划"
    )
    p.add_argument(
        "--recreate",
        action="store_true",
        help="迁移前先 drop collection 再建（默认仅 ensure）",
    )
    p.add_argument(
        "--verify",
        action="store_true",
        help="对比 FAISS / Qdrant Top-K 一致性（需先迁移）",
    )
    p.add_argument(
        "--queries",
        default="",
        help="一致性验证的 query 文件（--verify 必填）",
    )
    p.add_argument("--top-k", type=int, default=5)
    args = p.parse_args()

    if args.verify:
        if not args.queries:
            print("ERROR: --verify 必须配合 --queries", file=sys.stderr)
            return 2
        return cmd_verify(args)

    return cmd_migrate(args)


if __name__ == "__main__":
    raise SystemExit(main())
