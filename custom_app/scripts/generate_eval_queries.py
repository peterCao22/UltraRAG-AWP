"""Phase 8.1.3 —— Gemini 辅助生成评测候选问题。

思路：
    1. 从 KB 的 chunks.jsonl 抽样 N 个 chunk（轮询每个文档，覆盖度优先）
    2. 对每个 chunk 让 Gemini 编 K 个用户口吻的问题 + 一句话 gold answer
    3. 自动填入 relevant_chunk_ids = [chunk_id]；gold_answer 由模型给
    4. 输出 jsonl 作为人工筛选输入

用法：
    export GOOGLE_API_KEY=...
    python -m custom_app.scripts.generate_eval_queries --kb agv_demo --num 30 --output data/eval/agv_demo_gen.jsonl

设计取舍：
    - 简化为「一 chunk = N 问题」；人工筛 50%+ 是 PLAN §六.8.1.3 的预期
    - 不调 function calling；纯文本 prompt + JSON 模式解析
    - 失败的 chunk 跳过、继续下一个；不重试以省配额
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from custom_app.services.llm_adapter import (
    GeminiLLMAdapter,
    GeminiServiceUnavailable,
    gemini_response_extract_text,
)

load_dotenv()

_logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """你是一个工业 SOP 知识库的评测集构造助手。
给你一段知识库片段（chunk），请生成若干个**最有可能被一线运维 / 操作员问到**的问题，
以及每个问题对应的**精简答案**（来自片段，≤80 字）。

要求：
1. 问题口语化、第一人称视角（"我应该怎么..."、"...怎么处理"），不要照抄片段里的小标题
2. 问题之间角度不同（操作步骤 / 故障原因 / 注意事项 / 名词解释 等）
3. 答案必须来自片段，不要编造；如片段太短无法答复 N 个高质量问题，就少出几个
4. 返回严格 JSON，键固定为 "questions"，值是数组，每个元素 {"query": "...", "gold_answer": "..."}

返回示例：
{"questions": [
  {"query": "AGV 卡在急停状态怎么处理？", "gold_answer": "检查两侧急停按钮，按下后顺时针旋转复位"},
  {"query": "复位急停按钮后需要做什么？", "gold_answer": "确认 PLS 灯熄灭，然后在控制台重新启用 AGV"}
]}"""


_JSON_RE = re.compile(r"\{[\s\S]*\}")


def _parse_questions(text: str) -> list[dict[str, str]]:
    """从 Gemini 返回文本里抠出 JSON 并解析。"""
    m = _JSON_RE.search(text)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    qs = data.get("questions") or []
    out: list[dict[str, str]] = []
    for q in qs:
        if not isinstance(q, dict):
            continue
        query = (q.get("query") or "").strip()
        gold = (q.get("gold_answer") or "").strip()
        if query and gold:
            out.append({"query": query, "gold_answer": gold})
    return out


def _load_chunks(kb_root: Path) -> list[dict[str, Any]]:
    path = kb_root / "corpora" / "chunks.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"chunks file not found: {path}")
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _sample_chunks_round_robin(
    chunks: list[dict[str, Any]], n: int, *, seed: int = 42
) -> list[dict[str, Any]]:
    """按 doc 分组，轮询取样，让每个 doc 至少出现一次（覆盖度优先）。"""
    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in chunks:
        by_doc[c.get("doc") or "_unknown"].append(c)

    rng = random.Random(seed)
    for doc in by_doc:
        rng.shuffle(by_doc[doc])

    docs = sorted(by_doc.keys())
    rng.shuffle(docs)
    picked: list[dict[str, Any]] = []
    cursors = {d: 0 for d in docs}
    while len(picked) < n and any(cursors[d] < len(by_doc[d]) for d in docs):
        for d in docs:
            if cursors[d] < len(by_doc[d]):
                picked.append(by_doc[d][cursors[d]])
                cursors[d] += 1
                if len(picked) >= n:
                    break
    return picked


def _chunk_text_for_prompt(chunk: dict[str, Any], *, max_chars: int = 1800) -> str:
    """构造发给 Gemini 的 chunk 上下文（去掉 [IMG: ...] 占位行，限长）。"""
    body = (chunk.get("contents") or "").strip()
    body = "\n".join(
        ln for ln in body.splitlines() if not ln.strip().startswith("[IMG:")
    )
    return body[:max_chars]


def generate_candidates(
    kb_id: str,
    *,
    num_chunks: int,
    per_chunk: int = 3,
    kb_root: Path | None = None,
    api_key: str | None = None,
    model: str | None = None,
    seed: int = 42,
) -> list[dict[str, Any]]:
    """生成评测候选。返回 dict 列表，调用方写入 jsonl。"""
    kb_root = kb_root or Path("data/kb") / kb_id
    api_key = api_key or os.environ.get("GOOGLE_API_KEY") or os.environ.get(
        "ULTRARAG_GEMINI_API_KEY"
    )
    if not api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY (or ULTRARAG_GEMINI_API_KEY) is not set in environment"
        )
    model = model or os.environ.get("ULTRARAG_GEMINI_MODEL", "gemini-2.0-flash")

    chunks = _load_chunks(kb_root)
    if not chunks:
        raise RuntimeError(f"no chunks in {kb_root}/corpora/chunks.jsonl")

    sampled = _sample_chunks_round_robin(chunks, num_chunks, seed=seed)
    _logger.info(
        "kb=%s total_chunks=%d sampled=%d per_chunk=%d model=%s",
        kb_id,
        len(chunks),
        len(sampled),
        per_chunk,
        model,
    )

    adapter = GeminiLLMAdapter(api_key=api_key, model=model)
    out: list[dict[str, Any]] = []
    counter = 0
    for chunk in sampled:
        chunk_id = chunk["id"]
        ctx = _chunk_text_for_prompt(chunk)
        if not ctx:
            _logger.info("skip empty chunk: %s", chunk_id)
            continue
        user_text = (
            f"知识库片段（chunk_id={chunk_id}，文档={chunk.get('doc')}）：\n"
            f"---\n{ctx}\n---\n"
            f"请按要求生成 {per_chunk} 个问题及答案，返回 JSON。"
        )
        try:
            resp = adapter.call(
                messages=[{"role": "user", "content": user_text}],
                system_prompt=SYSTEM_PROMPT,
                generation_config={"temperature": 0.7, "responseMimeType": "application/json"},
            )
            text = gemini_response_extract_text(resp)
        except GeminiServiceUnavailable as e:
            _logger.warning("gemini unavailable on chunk %s: %s", chunk_id, e)
            continue
        except Exception as e:  # noqa: BLE001 — 任何模型异常都跳过，不中断整次生成
            _logger.warning("gemini error on chunk %s: %s", chunk_id, e)
            continue

        questions = _parse_questions(text)
        if not questions:
            _logger.info("no valid questions parsed from chunk %s", chunk_id)
            continue
        for q in questions[:per_chunk]:
            counter += 1
            out.append(
                {
                    "id": f"eval_{kb_id}_gen_{counter:03d}",
                    "kb_id": kb_id,
                    "query": q["query"],
                    "relevant_chunk_ids": [chunk_id],
                    "gold_answer": q["gold_answer"],
                    "tags": ["from_gemini", f"doc:{chunk.get('doc')}"],
                    "source": "generated",
                }
            )
    return out


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kb", required=True, help="知识库 ID")
    p.add_argument(
        "--output",
        type=Path,
        required=True,
        help="输出 JSONL 路径（如 data/eval/agv_demo_gen.jsonl）",
    )
    p.add_argument(
        "--num-chunks", type=int, default=15, help="抽样多少个 chunk 让 Gemini 编题"
    )
    p.add_argument(
        "--per-chunk", type=int, default=3, help="每个 chunk 编几道题"
    )
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--model", default=None, help="Gemini 模型名，缺省读 env")
    args = p.parse_args(argv)

    rows = generate_candidates(
        kb_id=args.kb,
        num_chunks=args.num_chunks,
        per_chunk=args.per_chunk,
        seed=args.seed,
        model=args.model,
    )
    if not rows:
        _logger.warning("no candidates generated for kb=%s", args.kb)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    _logger.info("wrote %d candidates to %s", len(rows), args.output)
    _logger.info(
        "NOTE: 这些 query/gold_answer 由 Gemini 编；人工筛选率应 ≥50%% 才能进终版"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
