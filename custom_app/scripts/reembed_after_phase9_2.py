"""Phase 9.2 一次性脚本：仅重 embed + index + qdrant，**不重 parse**。

为什么需要这个：
    - chunks.jsonl 已经被 Phase 9.1 image_describer 写入了 caption_zh /
      caption_en / entities
    - 完整 ingest 流程 (_run_ingest_job) 会先 parse → 清掉 chunks.jsonl 里
      的 caption（重新从 raw .docx 解析），白干 Phase 9.1
    - 这个脚本直接走 _embed_stage → _index_stage → _qdrant_stage，
      让新 compose_doc_embedding_text（含 image_block）重新算 embedding

用法：
    python -m custom_app.scripts.reembed_after_phase9_2 --kb agv_demo
    python -m custom_app.scripts.reembed_after_phase9_2 --kb ifs_docs

    # dry-run：只 verify 不动 embedding / Qdrant
    python -m custom_app.scripts.reembed_after_phase9_2 --kb agv_demo --dry-run

退出码：
    0 成功 / dry-run 完成
    1 失败（chunks.jsonl 缺失 / Qdrant 不可达等）
    2 用法错误
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

_logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kb", required=True, help="KB ID（如 agv_demo / ifs_docs）")
    p.add_argument("--dry-run", action="store_true",
                  help="只验证 chunks.jsonl 含 caption，不动 embedding / Qdrant")
    args = p.parse_args(argv)

    kb_root = Path(f"data/kb/{args.kb}")
    chunks_path = kb_root / "corpora" / "chunks.jsonl"
    embedding_path = kb_root / "embedding" / "embedding.npy"
    index_path = kb_root / "index" / "index.index"

    if not chunks_path.exists():
        _logger.error("chunks file not found: %s", chunks_path)
        return 1

    print(f"=== Phase 9.2 re-embed — kb={args.kb} ===")
    print(f"chunks file: {chunks_path}")
    print(f"embedding output: {embedding_path}")
    print(f"index output: {index_path}")

    # 统计 chunks 含 caption 的图片数量（验证 Phase 9.1 已落地）
    chunks = [
        json.loads(line)
        for line in chunks_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    total_imgs = 0
    with_caption = 0
    failed = 0
    for c in chunks:
        for img in c.get("images") or []:
            if not isinstance(img, dict):
                continue
            total_imgs += 1
            if img.get("_describe_failed"):
                failed += 1
            elif str(img.get("caption_zh") or "").strip() or str(img.get("caption_en") or "").strip():
                with_caption += 1
    print(f"chunks: {len(chunks)}")
    print(f"images: total={total_imgs}, with_caption={with_caption}, failed={failed}")
    if total_imgs > 0:
        pct = with_caption * 100 / total_imgs
        print(f"caption coverage: {pct:.0f}%")

    if total_imgs > 0 and with_caption == 0:
        print("\n[WARN] 所有图片都没有 caption！Phase 9.1 backfill 未跑过？")
        print("       请先跑：python -m custom_app.scripts.backfill_image_captions --kb " + args.kb)
        return 1

    if args.dry_run:
        print("\n[dry-run] 不执行 embedding / Qdrant 更新。")
        return 0

    # Stage 1: embed（chunks.jsonl → embedding.npy）
    print("\n[1/3] 重新生成 embedding.npy...")
    from custom_app.services.google_embedder import build_embedding_npy
    embedding_path.parent.mkdir(parents=True, exist_ok=True)
    build_embedding_npy(str(chunks_path), str(embedding_path))
    print(f"  done: {embedding_path}")

    # Stage 2: FAISS index（向后兼容）
    print("\n[2/3] 重建 FAISS index...")
    import numpy as np
    import faiss
    emb = np.load(str(embedding_path))
    if emb.ndim != 2 or emb.shape[0] == 0:
        _logger.error("embedding matrix is empty; check chunks.jsonl")
        return 1
    ids = np.arange(emb.shape[0]).astype(np.int64)
    emb = np.asarray(emb, dtype=np.float32, order="C")
    cpu_flat = faiss.IndexFlatIP(emb.shape[1])
    cpu_index = faiss.IndexIDMap2(cpu_flat)
    cpu_index.add_with_ids(emb, ids)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    faiss.write_index(cpu_index, str(index_path))
    print(f"  done: {index_path}  ({emb.shape[0]} vectors, dim={emb.shape[1]})")

    # Stage 3: Qdrant upsert（主向量库）
    print("\n[3/3] 重建 Qdrant collection...")
    from custom_app.services.vectorstore.qdrant_store import QdrantVectorStore
    chunk_ids = [str(c.get("id", "")) for c in chunks]
    payloads = [
        {
            "kb_id": args.kb,
            "doc": c.get("doc", ""),
            "source_type": c.get("source_type", "unknown"),
            "parser": c.get("parser", "unknown"),
            "chunk_data": c,
        }
        for c in chunks
    ]
    store = QdrantVectorStore(kb_id=args.kb, embed_dim=emb.shape[1])
    store.ensure_collection(recreate=True)  # Phase 9.2 重新打底
    store.upsert(chunk_ids, emb, payloads)
    final_size = store.size()
    print(f"  done: collection size={final_size}")

    print(f"\n✓ Phase 9.2 重 embed 完成。kb={args.kb}")
    print("  下一步：跑 eval_custom_app 对比 Phase 8.1 基线")
    return 0


if __name__ == "__main__":
    sys.exit(main())
