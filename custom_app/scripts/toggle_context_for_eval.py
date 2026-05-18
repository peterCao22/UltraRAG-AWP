"""Phase 8.2.3 评测专用：临时切换 chunks.jsonl 的 context 字段开关。

用法：
    # 把 context 字段重命名为 _context_disabled（让 embedder 看不到它）
    python -m custom_app.scripts.toggle_context_for_eval --kb agv_demo --off

    # 还原 context 字段
    python -m custom_app.scripts.toggle_context_for_eval --kb agv_demo --on

切换后必须重建 embedding + Qdrant 才生效（脚本不重建，由调用方负责）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


def toggle_chunks_context(chunks_path: Path, *, enable: bool) -> tuple[int, int]:
    """切换 context 字段的可见性。enable=False 把 context 改名 _context_disabled。"""
    rows = []
    with chunks_path.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            rows.append(json.loads(s))

    flipped = 0
    for r in rows:
        if enable:
            # 还原：_context_disabled → context
            if "_context_disabled" in r:
                r["context"] = r.pop("_context_disabled")
                flipped += 1
        else:
            # 关闭：context → _context_disabled
            if (r.get("context") or "").strip():
                r["_context_disabled"] = r.pop("context")
                flipped += 1

    tmp = chunks_path.with_suffix(chunks_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(chunks_path)
    return len(rows), flipped


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kb", required=True)
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--off", action="store_true", help="把 context 隐藏")
    grp.add_argument("--on", action="store_true", help="还原 context")
    args = p.parse_args(argv)

    chunks = Path(f"data/kb/{args.kb}/corpora/chunks.jsonl")
    if not chunks.exists():
        print(f"NOT FOUND: {chunks}")
        return 1

    total, flipped = toggle_chunks_context(chunks, enable=args.on)
    action = "enabled" if args.on else "disabled"
    print(f"{args.kb}: {action} context on {flipped}/{total} chunks")
    print("NOTE: must rebuild embedding + Qdrant to take effect")
    return 0


if __name__ == "__main__":
    sys.exit(main())
