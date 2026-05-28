"""Phase 12.1 指代消解评测脚本。

跑数据集 `data/eval/phase12_1/reference_resolution_dataset.jsonl`，对每条样本调用
`reference_resolver.resolve_references(query, turns)`，对比 LLM 改写结果与 expected_rewrite。

指标（PHASE_12_1_PLAN.md §五.2）：
    精确率 = 改写正确 / 触发改写总数
    召回率 = 触发改写 / 应改写场景数
    F1    = 调和均值
    诱饵未触发率 = 不该改写的样本里确实没改写的比例

判等策略：
    1. 字面 normalize（去标点、去空格、小写）后相等 → 算正确
    2. 否则用 LLM 判等价（与 expected_rewrite 比对，避免字面不同但语义相同被误判）
       —— 默认开启，可用 --no-llm-judge 跳过（节省成本）

用法：
    python -m custom_app.scripts.eval_reference_resolution
    python -m custom_app.scripts.eval_reference_resolution --output data/eval/phase12_1/baseline.json
    python -m custom_app.scripts.eval_reference_resolution --no-llm-judge   # 仅字面比较
    python -m custom_app.scripts.eval_reference_resolution --limit 10      # 跑前 N 条调试

env：
    与 reference_resolver 一致（ANTHROPIC_API_KEY / GOOGLE_API_KEY）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()
_logger = logging.getLogger(__name__)

DEFAULT_DATASET = Path("data/eval/phase12_1/reference_resolution_dataset.jsonl")


# ────────────────────────────────────────────────────────────────────────────
# 判等
# ────────────────────────────────────────────────────────────────────────────


_PUNCT_RE = re.compile(r"[，。！？、；：,.!?;:\s]+")


def _normalize(text: str) -> str:
    """字面 normalize：去标点 + 去空格 + 小写。"""
    if not text:
        return ""
    return _PUNCT_RE.sub("", text.lower())


def _literal_equal(a: str, b: str) -> bool:
    return _normalize(a) == _normalize(b)


def _resolve_judge_credentials(
    prefer_model: str = "claude-sonnet-4-6",
) -> tuple[str, str]:
    """优先 DB 然后 env；返回 (api_key, model_name)。

    judge 用更强模型（Sonnet > Haiku）以避免对等价改写误判 no。
    """
    # 先 DB（admin 后台配的模型条目）
    try:
        from custom_app.repositories.chat_model_repository import ChatModelRepository
        rows = ChatModelRepository().list_active(tenant_id=1)
        an_rows = [
            r for r in rows
            if (r.get("provider") or "").strip() == "anthropic"
        ]
        # 优先取 prefer_model（默认 Sonnet）
        for r in an_rows:
            if (r.get("model_name") or "").strip() == prefer_model:
                key = (r.get("api_key") or "").strip()
                if key:
                    return key, prefer_model
        # 没 prefer，取第一条
        if an_rows:
            key = (an_rows[0].get("api_key") or "").strip()
            model = (an_rows[0].get("model_name") or "").strip() or prefer_model
            if key:
                return key, model
    except Exception as e:  # noqa: BLE001
        _logger.warning("DB chat_models lookup failed: %s", e)
    # fallback env
    api_key = (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ULTRARAG_ANTHROPIC_API_KEY")
        or ""
    ).strip()
    return api_key, prefer_model


def _llm_judge_equivalent(
    rewritten: str,
    expected: str,
    *,
    query: str,
    api_key: str,
    model: str = "claude-haiku-4-5-20251001",
) -> bool:
    """用 LLM 判两个 query 是否语义等价。失败时保守返回 False。"""
    try:
        from anthropic import Anthropic  # type: ignore
    except ImportError:
        return False
    prompt = (
        "你是检索 query 等价性裁判。给两个改写后的 query A 和 B，"
        "判断它们对**向量检索系统**而言是否能召回相同的文档。\n\n"
        "判等标准（满足任一就算 yes）：\n"
        "1. 核心主题/对象一致（如「急停按钮」vs「AGV 急停按钮」一致，仅多/少限定词）\n"
        "2. 核心动词意图一致（如「检查」vs「操作」差异较大→ no，「多久检查」vs「检查频率」→ yes）\n"
        "3. 核心实体一致 + 问题方向一致（如「Loop Emergency 怎么解除」vs「Loop Emergency SOP 怎么解除」→ yes，多个 SOP 限定属于同义）\n"
        "4. 一个比另一个更精确但同领域（如「5 项配置在哪里设置」vs「出库/入库/品牌/零件/计划人这5项配置在哪里设置」→ yes）\n\n"
        "判 no 的情况：\n"
        "- 核心动词不一致（「检查」vs「配置」、「操作」vs「定义」）\n"
        "- 核心实体不同（「急停按钮」vs「电池块」）\n"
        "- 问题方向不同（一个问操作步骤、一个问原理）\n\n"
        f"原始用户问题：{query}\n"
        f"A: {rewritten}\n"
        f"B: {expected}\n\n"
        "只输出 yes 或 no（小写），不要解释。"
    )
    try:
        client = Anthropic(api_key=api_key, timeout=15)
        resp = client.messages.create(
            model=model,
            max_tokens=4,
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content or []:
            text = (getattr(block, "text", None) or "").strip().lower()
            if text.startswith("yes"):
                return True
            if text.startswith("no"):
                return False
    except Exception as e:  # noqa: BLE001
        _logger.warning("llm_judge failed: %s", e)
    return False


# ────────────────────────────────────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────────────────────────────────────


def _load_dataset(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _evaluate(
    rows: list[dict],
    *,
    use_llm_judge: bool = True,
    llm_judge_key: str | None = None,
    llm_judge_model: str = "claude-haiku-4-5-20251001",
    limit: int | None = None,
) -> dict[str, Any]:
    """跑评测，返回 report dict。"""
    from custom_app.services.reference_resolver import resolve_references

    if limit:
        rows = rows[:limit]

    # 计数
    n_total = len(rows)
    # 按 should_rewrite 分桶
    n_pos = sum(1 for r in rows if r["should_rewrite"])   # 应改写
    n_neg = n_total - n_pos                                # 诱饵

    # 计数：触发改写 / 改写正确 / 诱饵被误触发
    tp = 0   # 应改写 + 模型改写 + 改写正确
    fp = 0   # 不该改写 + 模型却改写了
    fn = 0   # 应改写 + 模型没改 (skip)
    tn = 0   # 不该改写 + 模型也没改
    rewrite_wrong = 0   # 应改写 + 模型改写但内容错（与 expected 不等价）

    per_item_records: list[dict] = []
    per_category: dict[str, Counter] = {}

    for i, sample in enumerate(rows, 1):
        sid = sample["id"]
        category = sample.get("category", "unknown")
        turns = sample.get("turns", [])
        query = sample["query"]
        expected = sample.get("expected_rewrite") or query
        should = bool(sample["should_rewrite"])

        # 调 resolver
        result = resolve_references(query, turns)
        applied = result.applied
        rewritten = result.rewritten_query

        # 判等：当应改写 + 模型改写了，要看改写对不对
        match_method = None
        is_correct_rewrite = False
        if applied:
            if _literal_equal(rewritten, expected):
                is_correct_rewrite = True
                match_method = "literal"
            elif use_llm_judge and llm_judge_key:
                is_correct_rewrite = _llm_judge_equivalent(
                    rewritten, expected, query=query,
                    api_key=llm_judge_key, model=llm_judge_model,
                )
                match_method = "llm_judge"

        # 分类
        if should and applied and is_correct_rewrite:
            tp += 1
            outcome = "tp_correct_rewrite"
        elif should and applied and not is_correct_rewrite:
            tp += 1  # 触发了改写算 trigger 正确（用于精确率分母里的 trigger 计数）
            rewrite_wrong += 1
            outcome = "tp_wrong_content"  # 触发对但改写内容错
        elif should and not applied:
            fn += 1
            outcome = "fn_missed"
        elif not should and applied:
            fp += 1
            outcome = "fp_false_trigger"
        else:  # not should and not applied
            tn += 1
            outcome = "tn_correct_skip"

        per_category.setdefault(category, Counter())[outcome] += 1
        per_item_records.append({
            "id": sid,
            "category": category,
            "query": query,
            "expected_rewrite": expected,
            "applied": applied,
            "rewritten": rewritten,
            "confidence": result.confidence,
            "should_rewrite": should,
            "correct_rewrite": is_correct_rewrite,
            "outcome": outcome,
            "match_method": match_method,
            "ms": result.ms,
            "skip_reason": result.skip_reason,
            "model": result.model,
        })

        if i % 5 == 0 or i == len(rows):
            _logger.info("evaluated %d/%d", i, len(rows))

    # 指标
    triggered = tp + fp   # 模型触发改写总数
    precision = (tp - rewrite_wrong) / triggered if triggered else 0.0   # 触发且改写正确 / 触发总数
    recall = (tp - rewrite_wrong) / n_pos if n_pos else 0.0              # 触发且改写正确 / 应改写总数
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    decoy_skip_rate = tn / n_neg if n_neg else 1.0
    # 触发正确率（不考虑内容是否正确）
    trigger_recall = tp / n_pos if n_pos else 0.0

    return {
        "n_total": n_total,
        "n_should_rewrite": n_pos,
        "n_decoy": n_neg,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "rewrite_wrong": rewrite_wrong,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "decoy_skip_rate": decoy_skip_rate,
        "trigger_recall": trigger_recall,
        "per_category": {c: dict(cnt) for c, cnt in per_category.items()},
        "items": per_item_records,
    }


def _print_report(report: dict[str, Any]) -> None:
    print()
    print("=== Phase 12.1 Reference Resolution Evaluation ===")
    print(f"Total samples:       {report['n_total']}")
    print(f"  should_rewrite=Y:  {report['n_should_rewrite']}")
    print(f"  decoy:             {report['n_decoy']}")
    print()
    print(f"Trigger counts:")
    print(f"  TP (triggered):    {report['tp']}  (of which {report['rewrite_wrong']} had wrong rewrite content)")
    print(f"  FP (false trigger):{report['fp']}")
    print(f"  FN (missed):       {report['fn']}")
    print(f"  TN (correct skip): {report['tn']}")
    print()
    print("Metrics:")
    print(f"  Precision:         {report['precision']:.4f}")
    print(f"  Recall:            {report['recall']:.4f}")
    print(f"  F1:                {report['f1']:.4f}")
    print(f"  Decoy skip rate:   {report['decoy_skip_rate']:.4f}")
    print(f"  Trigger recall:    {report['trigger_recall']:.4f}  (含改写内容错的触发)")
    print()
    print("Per category:")
    for c, cnt in sorted(report["per_category"].items()):
        total = sum(cnt.values())
        outcomes_str = ", ".join(f"{k}={v}" for k, v in sorted(cnt.items()))
        print(f"  {c:24s} n={total}  {outcomes_str}")
    print()
    # 退出条件检查（PHASE_12_1_PLAN §十）
    print("Exit criteria:")
    p_ok = report["precision"] >= 0.80
    r_ok = report["recall"] >= 0.75
    d_ok = report["decoy_skip_rate"] >= 0.95
    print(f"  precision >= 0.80: {report['precision']:.4f}  {'✅' if p_ok else '❌'}")
    print(f"  recall    >= 0.75: {report['recall']:.4f}  {'✅' if r_ok else '❌'}")
    print(f"  decoy skip >= 0.95:{report['decoy_skip_rate']:.4f}  {'✅' if d_ok else '❌'}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dataset", type=Path, default=DEFAULT_DATASET,
        help=f"评测集路径（默认 {DEFAULT_DATASET}）",
    )
    p.add_argument(
        "--output", type=Path, default=None,
        help="保存详细报告 JSON（含 per-item 记录）。默认不保存",
    )
    p.add_argument(
        "--no-llm-judge", action="store_true",
        help="不用 LLM 判等价（仅字面比较，省成本但精确率偏低）",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="只跑前 N 条（调试用）",
    )
    args = p.parse_args(argv)

    if not args.dataset.exists():
        _logger.error("dataset not found: %s", args.dataset)
        return 1

    rows = _load_dataset(args.dataset)
    _logger.info("loaded %d items from %s", len(rows), args.dataset)

    use_llm_judge = not args.no_llm_judge
    llm_judge_key = ""
    llm_judge_model = "claude-haiku-4-5-20251001"
    if use_llm_judge:
        llm_judge_key, llm_judge_model = _resolve_judge_credentials()

    if use_llm_judge and not llm_judge_key:
        _logger.warning(
            "LLM judge enabled but no Anthropic key (env or DB); falling back to literal-only",
        )
        use_llm_judge = False
    elif use_llm_judge:
        _logger.info("LLM judge model=%s key=%s***", llm_judge_model, llm_judge_key[:6])

    report = _evaluate(
        rows,
        use_llm_judge=use_llm_judge,
        llm_judge_key=llm_judge_key,
        llm_judge_model=llm_judge_model,
        limit=args.limit,
    )
    report["meta"] = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset_path": str(args.dataset),
        "use_llm_judge": use_llm_judge,
        "limit": args.limit,
    }

    _print_report(report)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        _logger.info("wrote report to %s", args.output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
