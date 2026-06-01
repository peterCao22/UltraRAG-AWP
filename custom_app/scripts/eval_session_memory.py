"""Phase 12.2 Session Memory 多轮评测脚本。

目标：验证"摘要前后回答的 F1 一致性，不应下降"（方案 §四.2 退出条件）。

设计（MVP，复用 agv_demo eval 数据集）：
    1. 选 ``tags: ["from_session"]`` 的样本（agv_demo 里前 8 条 BatteryChange 流程）
    2. 把它们当作 1 个虚拟 session 顺序跑
    3. 模式 A baseline：每条独立调用，无 history、无 summary（与 Phase 11.x 行为一致）
    4. 模式 B with_memory：第 N 条起触发 maybe_summarize；后续轮注入 summary + recent K
    5. 对比两模式各自 retrieval Hit@K

退出码：
    0  评测完成；Hit@K 不回归（或上涨）
    1  Hit@K 回归 > 5%
    2  环境/数据问题

用法：
    python -m custom_app.scripts.eval_session_memory --kb agv_demo --top-k 5
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)


def _load_dataset(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"dataset not found: {path}")
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        items.append(json.loads(line))
    return items


def _select_session_samples(
    items: list[dict[str, Any]], min_size: int = 6,
) -> list[dict[str, Any]]:
    """挑 tags 含 from_session 的样本作为多轮模拟。

    agv_demo 数据集前 8 条 (eval_agv_demo_001..008) 是 BatteryChangeSequenceSOP
    的连续步骤问答，天然适合做多轮评测。
    """
    session_items = [it for it in items if "from_session" in (it.get("tags") or [])]
    if len(session_items) < min_size:
        raise ValueError(
            f"too few from_session samples: {len(session_items)} < {min_size}"
        )
    return session_items


def _retrieve_chunk_ids(runner, query: str, *, top_k: int,
                       history: list[dict[str, Any]] | None = None,
                       session_id: str | None = None) -> list[str]:
    """跑一次检索；返回 chunk_id 列表。"""
    prep = runner._prepare_chat_context(
        query, top_k=top_k, agent_mode="quick",
        history=history, session_id=session_id,
    )
    rows = getattr(runner, "_rows", []) or []
    ids: list[str] = []
    for idx in prep["hit_ids"][:top_k]:
        if 0 <= idx < len(rows):
            cid = str(rows[idx].get("id", "")).strip()
            if cid:
                ids.append(cid)
    return ids


def _hit_at_k(retrieved: list[str], gold: list[str]) -> bool:
    """gold 中任意一条出现在 retrieved 即算命中。"""
    gset = set(gold)
    return any(r in gset for r in retrieved)


def _build_session_with_msgs(session_id: str, kb_id: str,
                             turns: list[tuple[str, str]]) -> None:
    """直接在 DB 写一个测试 session 和对应消息（user/assistant 交替）。"""
    from custom_app.repositories.session_repository import SessionRepository
    from custom_app.db import now_iso

    repo = SessionRepository()
    ts = now_iso()
    # 已存在则清掉重建
    if repo.get_session(session_id):
        repo.delete_session(session_id)
    repo.create_session(
        session_id=session_id, kb_id=kb_id, title="eval session memory",
        agent_mode="quick", created_at=ts,
    )
    for role, content in turns:
        if role == "user":
            repo.append_user_message(session_id, content=content, created_at=ts)
        else:
            repo.append_assistant_message(
                session_id, content=content, reasoning_json="{}", created_at=ts,
            )


def _cleanup_session(session_id: str) -> None:
    from custom_app.repositories.session_repository import SessionRepository
    try:
        SessionRepository().delete_session(session_id)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kb", default="agv_demo", help="KB ID")
    p.add_argument("--dataset", type=Path, default=None)
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--window", type=int, default=4,
                  help="ULTRARAG_SESSION_MEMORY_WINDOW 临时覆盖（默认 4 让摘要在 5 轮后触发）")
    p.add_argument("--no-llm-summary", action="store_true",
                  help="跳过 maybe_summarize（仅评测 history-in-prompt 效果，不调 Haiku）")
    args = p.parse_args(argv)

    dataset_path = args.dataset or Path(f"data/eval/{args.kb}.jsonl")
    try:
        items = _load_dataset(dataset_path)
    except FileNotFoundError as e:
        _logger.error("%s", e)
        return 2
    try:
        session_items = _select_session_samples(items)
    except ValueError as e:
        _logger.error("%s", e)
        return 2

    print(f"=== Phase 12.2 多轮评测 — kb={args.kb} top-k={args.top_k} ===")
    print(f"dataset: {dataset_path}")
    print(f"session 样本数: {len(session_items)}")
    print(f"WINDOW（临时）: {args.window}")
    print()

    # 临时覆盖 env（用 monkeypatch 不行，env 是模块级；用 os.environ 直接覆盖）
    if args.window:
        os.environ["ULTRARAG_SESSION_MEMORY_WINDOW"] = str(args.window)
    os.environ["ULTRARAG_SESSION_MEMORY_RECENT_K"] = "6"

    from custom_app.services.rag_runner import RagRunner
    from custom_app.services.session_memory import maybe_summarize

    runner = RagRunner(kb_id=args.kb)
    runner.init()

    # ── 模式 A：baseline（每条独立） ──
    print("[Mode A] baseline — 每条独立调用，无 history/summary")
    hits_a = 0
    for i, it in enumerate(session_items, start=1):
        retrieved = _retrieve_chunk_ids(runner, it["query"], top_k=args.top_k)
        gold = it.get("relevant_chunk_ids") or []
        hit = _hit_at_k(retrieved, gold)
        hits_a += int(hit)
        print(f"  [{i:2d}] hit={int(hit)} q={it['query'][:50]!r}")
    print(f"  Hit@{args.top_k} = {hits_a}/{len(session_items)} = {hits_a/len(session_items):.4f}")
    print()

    # ── 模式 B：with_memory（顺序跑 + summary） ──
    print(f"[Mode B] with_memory — 顺序跑，第 {args.window+1} 条起注入 summary + recent {args.window} 轮")
    session_id = f"eval_phase12_2_{uuid.uuid4().hex[:8]}"
    try:
        _build_session_with_msgs(session_id, args.kb, turns=[])
        hits_b = 0
        prior_turns: list[dict[str, Any]] = []  # 已发生轮次（user/assistant 拼对）
        from custom_app.repositories.session_repository import SessionRepository
        from custom_app.db import now_iso
        repo = SessionRepository()

        for i, it in enumerate(session_items, start=1):
            ts = now_iso()
            # 用历史 + summary 跑检索
            retrieved = _retrieve_chunk_ids(
                runner, it["query"], top_k=args.top_k,
                history=prior_turns, session_id=session_id,
            )
            gold = it.get("relevant_chunk_ids") or []
            hit = _hit_at_k(retrieved, gold)
            hits_b += int(hit)
            # 把本轮 user + 假装 assistant 答案写入 history 和 DB
            # （为简化，assistant 答案用 gold_answer；真实场景应是 LLM 生成）
            assistant_text = (it.get("gold_answer") or "").strip() or "（无答案）"
            repo.append_user_message(session_id, content=it["query"], created_at=ts)
            repo.append_assistant_message(
                session_id, content=assistant_text,
                reasoning_json="{}", created_at=ts,
            )
            prior_turns.append({"role": "user", "content": it["query"]})
            prior_turns.append({"role": "assistant", "content": assistant_text})
            # 触发 maybe_summarize
            mem_label = "skip"
            if not args.no_llm_summary:
                t0 = time.perf_counter()
                mem = maybe_summarize(session_id)
                mem_ms = int((time.perf_counter() - t0) * 1000)
                if mem.applied:
                    mem_label = f"applied chars={len(mem.summary)} ms={mem_ms}"
                else:
                    mem_label = f"skip:{mem.skip_reason}"
            print(f"  [{i:2d}] hit={int(hit)} mem={mem_label} q={it['query'][:50]!r}")
        print(f"  Hit@{args.top_k} = {hits_b}/{len(session_items)} = {hits_b/len(session_items):.4f}")
    finally:
        _cleanup_session(session_id)

    print()
    print("=== 对比 ===")
    rate_a = hits_a / len(session_items)
    rate_b = hits_b / len(session_items)
    delta = rate_b - rate_a
    print(f"baseline:    Hit@{args.top_k} = {rate_a:.4f}")
    print(f"with_memory: Hit@{args.top_k} = {rate_b:.4f}")
    print(f"delta:       {delta:+.4f}")
    print()
    if delta < -0.05:
        print(f"[FAIL] 回归 > 5%（{delta:.4f}），Phase 12.2 接入有问题")
        return 1
    if delta >= 0:
        print(f"[OK] 不回归（{delta:+.4f}）")
    else:
        print(f"[WARN] 轻微下降（{delta:+.4f}），但在 5% 容忍范围内")
    return 0


if __name__ == "__main__":
    sys.exit(main())
