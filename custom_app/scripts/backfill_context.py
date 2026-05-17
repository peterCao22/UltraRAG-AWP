"""Phase 8.2.1.e —— 给现有 KB 的 chunks.jsonl 回填 context 字段。

注意：仅写 chunks.jsonl 的 `context` 字段；**不重建 embedding / Qdrant**。
后续要让 context 影响检索，还需触发一次 ingest（force_reindex）或单独跑
build_embedding_npy + Qdrant upsert。

用法：
    python -m custom_app.scripts.backfill_context --kb agv_demo
    python -m custom_app.scripts.backfill_context --kb ifs_docs --force

env：
    GOOGLE_API_KEY                 必填
    ULTRARAG_GEMINI_MODEL          可选，默认 gemini-2.0-flash
    ULTRARAG_DISABLE_CONTEXTUAL=1  跳过（脚本退出码 0 + 警告）
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
_logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kb", required=True, help="知识库 ID（如 agv_demo / ifs_docs）")
    p.add_argument(
        "--chunks",
        type=Path,
        default=None,
        help="覆盖默认路径（默认 data/kb/<kb>/corpora/chunks.jsonl）",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="忽略已有 context，全部重新生成（默认幂等跳过）",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=4,
        help="并发数（默认 4）",
    )
    args = p.parse_args(argv)

    if os.environ.get("ULTRARAG_DISABLE_CONTEXTUAL", "").strip().lower() in (
        "1", "true", "yes"
    ):
        _logger.warning("ULTRARAG_DISABLE_CONTEXTUAL=1, exiting without action")
        return 0

    chunks_path = args.chunks or (
        Path("data/kb") / args.kb / "corpora" / "chunks.jsonl"
    )
    if not chunks_path.exists():
        _logger.error("chunks file not found: %s", chunks_path)
        return 1

    from custom_app.services.chunking.contextual import ContextEnricher

    try:
        enricher = ContextEnricher(max_workers=args.workers)
    except RuntimeError as e:
        _logger.error("failed to init ContextEnricher: %s", e)
        return 2

    def _progress(done: int, total: int) -> None:
        if done == 1 or done == total or done % 5 == 0:
            _logger.info("context progress: %d/%d", done, total)

    n_gen, n_skip, n_fail = enricher.enrich_chunks_jsonl(
        chunks_path, force=args.force, progress_cb=_progress
    )
    _logger.info(
        "DONE kb=%s generated=%d skipped=%d failed=%d", args.kb, n_gen, n_skip, n_fail
    )
    _logger.info(
        "提示：context 已写入 %s。要让检索看到这层信息，仍需重建 embedding + Qdrant。",
        chunks_path,
    )
    return 0 if n_fail == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
