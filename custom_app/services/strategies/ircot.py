"""Phase 8.3 Week 1 IRCoT 多轮检索 + 推理链。

设计来自 PHASE_8_3_KICKOFF.md §二.3 决策（不借 UltraRAG，直接自写）：
- 复用 RagRunner 的 _prepare_chat_context（拿 hit_ids + 走完 reranker / hybrid / RRF）
- 复用 RagRunner._rows 反查 chunk 元数据
- 复用 RagRunner._generate 调 LLM（按已配置的 chat backend）

流程：
    第 1 轮：基础检索 + Gemini 一步思考
    第 2..N 轮：用上一轮思考的首句再检索 → 新 chunks 累加 → 再一步思考
    终止条件：思考含 "答案是" / "因此" / "so the answer is" → 直接提取
              或 达到 max_loops → 用最后一次 LLM 输出作答案

与 RagRunner.chat 相比的两个差异：
    1. prompt 不同（用 prompt/ircot_sop.jinja 而不是 agv_qa_rag.jinja）
    2. 多轮：扩大 chunks_seen 集合直到模型给出最终答案

输出与 RagRunner.chat 兼容：含 answer / answer_blocks / sources / meta
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader

if TYPE_CHECKING:
    from custom_app.services.rag_runner import RagRunner

_logger = logging.getLogger(__name__)

# 中文 + 英文判终结的关键词
_END_PATTERNS = (
    "答案是",
    "因此答案",
    "所以答案",
    "最终答案",
    "so the answer is",
    "the answer is",
)

_FIRST_SENT_RE = re.compile(r"(.+?[。！？.!?])")
_FINAL_ANS_RE = re.compile(
    r"(?:答案是|因此答案|所以答案|最终答案|so the answer is|the answer is)[:：]?\s*(.+?)$",
    re.IGNORECASE | re.DOTALL,
)


def _has_end_marker(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(p.lower() in t for p in _END_PATTERNS)


def _extract_final_answer(text: str) -> str:
    """从含 "答案是: X" 的思考中抽 X；找不到返回原文。"""
    if not text:
        return ""
    m = _FINAL_ANS_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def _first_sentence(text: str) -> str | None:
    """抽首句作下一轮 query。"""
    if not text:
        return None
    # 去掉开头 "思考：" 标记
    t = re.sub(r"^\s*思考[:：]\s*", "", text.strip(), flags=re.MULTILINE)
    m = _FIRST_SENT_RE.search(t)
    return m.group(1).strip() if m else None


def _render_ircot_prompt(
    *,
    prompt_dir: Path,
    template_name: str,
    question: str,
    passages: list[dict[str, Any]],
    thoughts: list[str],
) -> str:
    """渲染 ircot_sop.jinja。"""
    env = Environment(loader=FileSystemLoader(str(prompt_dir)))
    tmpl = env.get_template(template_name)
    return tmpl.render(question=question, passages=passages, thoughts=thoughts)


def _build_passages_from_hits(rows: list[dict], hit_ids: list[int]) -> list[dict[str, Any]]:
    """从 RagRunner._rows + hit_ids 构造 prompt 用的 passages 列表。"""
    out = []
    for i in hit_ids:
        if i < 0 or i >= len(rows):
            continue
        row = rows[i]
        contents = (row.get("contents") or "").strip()
        # 去掉 [IMG: ...] 行（IRCoT 推理不需要图片占位）
        cleaned = "\n".join(
            ln for ln in contents.splitlines() if not ln.strip().startswith("[IMG:")
        )
        out.append(
            {
                "source_id": str(row.get("id", "")),
                "contents": cleaned,
            }
        )
    return out


def chat_ircot(
    rag_runner: "RagRunner",
    question: str,
    *,
    max_loops: int = 2,
    first_round_top_k: int = 5,
    next_round_top_k: int = 3,
    prompt_dir: Path | None = None,
    template_name: str = "ircot_sop.jinja",
) -> dict[str, Any]:
    """IRCoT 多轮检索 + 推理链。

    Args:
        rag_runner: 已 init() 的 RagRunner 实例（必须已加载 _rows / _vector_store）
        question: 用户问题
        max_loops: 最多几轮思考（含最终轮；论文常用 2-4）
        first_round_top_k: 第 1 轮检索 top_k
        next_round_top_k: 第 2+ 轮每次新增 top_k

    Returns:
        dict（与 RagRunner.chat 兼容）：
            answer: 最终答案文本
            thoughts: 各轮思考链
            n_loops: 实际跑了几轮
            chunks_seen: 全部累计 chunk_id 列表（保留检索顺序）
            meta: {ircot_strategy=True, n_loops, n_chunks, total_ms}
    """
    if rag_runner is None:
        raise ValueError("rag_runner is required")
    q = (question or "").strip()
    if not q:
        raise ValueError("question is empty")
    if max_loops < 1:
        raise ValueError(f"max_loops must be ≥1, got {max_loops}")

    prompt_dir = prompt_dir or rag_runner.prompt_dir
    rows = getattr(rag_runner, "_rows", None)
    if not rows:
        raise RuntimeError("rag_runner._rows is empty; did you call init()?")

    t_total_begin = time.perf_counter()
    chunks_seen_set: set[int] = set()
    chunks_seen_order: list[int] = []  # 保留检索顺序
    thoughts: list[str] = []
    final_text = ""
    last_thought = ""

    for loop_idx in range(max_loops):
        # 决定本轮 query 与 top_k
        if loop_idx == 0:
            round_q = q
            round_k = first_round_top_k
        else:
            # 用上一轮思考的首句作下一轮 query
            next_q = _first_sentence(last_thought)
            if not next_q:
                _logger.info("ircot loop %d: no first sentence in thought, stopping", loop_idx)
                break
            round_q = next_q
            round_k = next_round_top_k

        # 检索：复用 RagRunner._prepare_chat_context（含 hybrid/RRF/reranker）
        prep = rag_runner._prepare_chat_context(round_q, top_k=round_k)
        new_hits = prep.get("hit_ids") or []

        # 累加 chunks_seen（保留首次出现顺序）
        for i in new_hits:
            if i not in chunks_seen_set:
                chunks_seen_set.add(i)
                chunks_seen_order.append(i)

        # 渲染 prompt 含全部 chunks_seen + 全部历史 thoughts
        passages = _build_passages_from_hits(rows, chunks_seen_order)
        prompt_text = _render_ircot_prompt(
            prompt_dir=prompt_dir,
            template_name=template_name,
            question=q,
            passages=passages,
            thoughts=thoughts,
        )

        # 调 LLM
        thought = rag_runner._generate(prompt_text).strip()
        thoughts.append(thought)
        last_thought = thought

        _logger.info(
            "ircot loop %d/%d: round_q=%r round_k=%d new_hits=%d chunks_seen=%d thought_len=%d",
            loop_idx + 1, max_loops, round_q[:40], round_k, len(new_hits),
            len(chunks_seen_order), len(thought),
        )

        # 判终结
        if _has_end_marker(thought):
            final_text = _extract_final_answer(thought)
            _logger.info("ircot loop %d: end marker found, stopping", loop_idx + 1)
            break

    # 达到 max_loops 仍未明确终结：用最后一次思考作答
    if not final_text:
        final_text = _extract_final_answer(last_thought) if last_thought else ""
        if not final_text and thoughts:
            final_text = thoughts[-1]
        _logger.info("ircot: max_loops reached without explicit end marker; using last thought")

    total_ms = int((time.perf_counter() - t_total_begin) * 1000)

    # 构造 chunk_id 列表给 evaluation 用
    chunks_seen_ids = [
        str(rows[i].get("id", ""))
        for i in chunks_seen_order
        if 0 <= i < len(rows)
    ]

    return {
        "answer": final_text,
        "thoughts": thoughts,
        "n_loops": len(thoughts),
        "chunks_seen": chunks_seen_ids,
        "hit_ids": chunks_seen_order,
        "meta": {
            "ircot_strategy": True,
            "n_loops": len(thoughts),
            "n_chunks_seen": len(chunks_seen_ids),
            "total_ms": total_ms,
            "early_stopped": bool(final_text and _has_end_marker(thoughts[-1] if thoughts else "")),
        },
    }
