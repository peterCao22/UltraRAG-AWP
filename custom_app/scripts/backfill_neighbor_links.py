"""Phase 11.3 一次性回填脚本：给已存在的 chunks.jsonl 注入 prev/next_chunk_id。

设计：
    - 不重新 parse docx、不重 embed、不重 upsert Qdrant
    - 直接在 chunks.jsonl 上按 doc 分组，注入邻居链字段
    - 幂等：已有这两个字段时会按当前文件顺序重写，保证一致
    - 安全：写之前备份 chunks.jsonl.bak.<timestamp>

用法：
    # dry-run，看会改多少 chunk
    python -m custom_app.scripts.backfill_neighbor_links --kb agv_demo --dry-run
    python -m custom_app.scripts.backfill_neighbor_links --kb ifs_docs --dry-run

    # 真实写入（自动备份原文件）
    python -m custom_app.scripts.backfill_neighbor_links --kb agv_demo
    python -m custom_app.scripts.backfill_neighbor_links --kb ifs_docs

退出码：
    0  成功
    1  失败（文件缺失 / 写入异常）
    2  使用错误
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List


def _load_chunks(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"chunks file not found: {path}")
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _summary(chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """统计 doc 数、chunk 数、已有邻居字段比例。"""
    docs = {str(c.get("doc", "")) for c in chunks}
    with_prev = sum(1 for c in chunks if "prev_chunk_id" in c)
    with_next = sum(1 for c in chunks if "next_chunk_id" in c)
    return {
        "doc_count": len(docs),
        "chunk_count": len(chunks),
        "already_with_prev": with_prev,
        "already_with_next": with_next,
    }


def _write_chunks(chunks: List[Dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in chunks:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kb", required=True, help="KB ID（对应 data/kb/<kb> 目录）")
    p.add_argument(
        "--chunks-path",
        type=Path,
        default=None,
        help="自定义 chunks.jsonl 路径（默认 data/kb/<kb>/corpora/chunks.jsonl）",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印统计，不写文件",
    )
    args = p.parse_args(argv)

    chunks_path = args.chunks_path or Path(f"data/kb/{args.kb}/corpora/chunks.jsonl")
    try:
        chunks = _load_chunks(chunks_path)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    before = _summary(chunks)
    print(f"=== Phase 11.3 邻居链回填 — kb={args.kb} ===")
    print(f"chunks file: {chunks_path}")
    print(f"before: {before}")

    # 调统一注入函数（导入延后到这里，避免 import 失败也能看 dry-run usage）
    from custom_app.services.docx_parser import link_neighbors_in_place

    link_neighbors_in_place(chunks)
    after = _summary(chunks)
    print(f"after:  {after}")

    if args.dry_run:
        print("\n[dry-run] 不写文件。要真实写入请去掉 --dry-run。")
        return 0

    # 备份
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_path = chunks_path.with_suffix(f".jsonl.bak.{ts}")
    shutil.copy2(chunks_path, backup_path)
    print(f"\n已备份原文件 → {backup_path}")

    _write_chunks(chunks, chunks_path)
    print(f"已写回 {chunks_path}（{after['chunk_count']} chunks）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
