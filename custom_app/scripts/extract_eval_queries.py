"""Phase 8.1.2 —— 从 kb_session_messages 抽真实 user query 作为评测集 A 路输入。

输出 JSONL（**部分字段**，relevant_chunk_ids / gold_answer 留待人工标注后补全）：
    {"id": "eval_<kb>_<n>",
     "kb_id": "agv_demo",
     "query": "...",
     "relevant_chunk_ids": [],     # ← 人工补
     "gold_answer": "",            # ← 人工补
     "tags": ["from_session"],
     "source": "session"}

去重策略：
    - normalize（小写 + 去标点）后比较；首次出现保留
    - 仅取 role='user' 消息
    - 长度过滤：默认 4-300 字（短打招呼/长贴段都不要）

用法：
    python -m custom_app.scripts.extract_eval_queries --kb agv_demo --output data/eval/agv_demo_raw.jsonl
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from custom_app.repositories.base import adapt_sql, get_default_provider

_logger = logging.getLogger(__name__)


def _normalize_query(q: str) -> str:
    """简易归一化用于去重（保留中文字符，去掉空白/标点）。"""
    s = q.strip().lower()
    # 去掉常见空白与中英文标点
    drop = set(" \t\r\n，。！？、；：（）【】「」《》—…!?,.;:()[]{}\"'`")
    return "".join(ch for ch in s if ch not in drop)


def extract_queries(
    kb_id: str,
    *,
    min_len: int = 4,
    max_len: int = 300,
    limit: int | None = None,
) -> list[dict]:
    """从 kb_session_messages 抽 user 消息。

    返回每条 dict（包含 query/source/tags），尚未生成 id —— 由调用方按顺序编号。
    """
    prov = get_default_provider()
    sql = adapt_sql(
        """
        SELECT m.content, m.created_at
        FROM kb_sessions s
        JOIN kb_session_messages m ON s.session_id = m.session_id
        WHERE s.kb_id = ?
          AND m.role = 'user'
        ORDER BY m.id ASC
        """,
        prov,
    )
    with prov.connect() as conn:
        cur = conn.execute(sql, (kb_id,))
        rows = cur.fetchall()

    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        content = r["content"] if hasattr(r, "keys") else r[0]
        q = (content or "").strip()
        if not q:
            continue
        if len(q) < min_len or len(q) > max_len:
            continue
        norm = _normalize_query(q)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(
            {
                "query": q,
                "tags": ["from_session"],
                "source": "session",
            }
        )
        if limit is not None and len(out) >= limit:
            break
    return out


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kb", required=True, help="知识库 ID（如 agv_demo）")
    p.add_argument(
        "--output",
        type=Path,
        required=True,
        help="输出 JSONL 路径（如 data/eval/agv_demo_raw.jsonl）",
    )
    p.add_argument("--min-len", type=int, default=4, help="保留 query 的最小字符数")
    p.add_argument("--max-len", type=int, default=300, help="保留 query 的最大字符数")
    p.add_argument("--limit", type=int, default=None, help="最多导出多少条（默认全部）")
    p.add_argument("--id-prefix", default=None, help="ID 前缀（默认 eval_<kb>_）")
    args = p.parse_args(argv)

    queries = extract_queries(
        args.kb,
        min_len=args.min_len,
        max_len=args.max_len,
        limit=args.limit,
    )
    if not queries:
        _logger.warning("no queries extracted for kb=%s", args.kb)
        return 1

    prefix = args.id_prefix or f"eval_{args.kb}_"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        for i, q in enumerate(queries, start=1):
            row = {
                "id": f"{prefix}{i:03d}",
                "kb_id": args.kb,
                "query": q["query"],
                "relevant_chunk_ids": [],
                "gold_answer": "",
                "tags": q["tags"],
                "source": q["source"],
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    _logger.info("wrote %d queries to %s", len(queries), args.output)
    _logger.info(
        "NOTE: relevant_chunk_ids/gold_answer 仍为空；人工标注后才能进入评测"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
