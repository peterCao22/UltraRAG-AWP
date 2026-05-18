"""Phase 8.2.3 评测对比矩阵生成。

读取 4 组 × 2 KB = 8 个评测 JSON，输出：
- 控制台打印矩阵
- data/eval/phase8_2_comparison.md：带分析的对比报告
"""
from __future__ import annotations

import json
from pathlib import Path

GROUPS = [
    ("1_vector_noctx", "vector + no context", "phase8_2/{kb}__vector_noctx.json"),
    ("2_vector_ctx",   "vector + context",    "phase8_2/{kb}__vector_ctx.json"),
    ("3_hybrid_noctx", "hybrid + no context", "phase8_2/{kb}__hybrid_noctx.json"),
    ("4_hybrid_ctx",   "hybrid + context (production)", "baseline/{kb}_2026-05-18.json"),
]
KBS = ["agv_demo", "ifs_docs"]
EVAL_ROOT = Path("data/eval")


def load_results() -> dict:
    """返回 results[group_key][kb] = retrieval_metrics dict。"""
    out: dict = {}
    for gkey, _, path_tmpl in GROUPS:
        out[gkey] = {}
        for kb in KBS:
            p = EVAL_ROOT / path_tmpl.format(kb=kb)
            if p.exists():
                with p.open("r", encoding="utf-8") as f:
                    out[gkey][kb] = json.load(f)["retrieval_metrics"]
            else:
                out[gkey][kb] = {}
    return out


def fmt(m: dict, k: str) -> str:
    v = m.get(k)
    return f"{v:.4f}" if v is not None else "  -   "


def print_matrix(results: dict) -> None:
    head = f"{'group':<32} | {'KB':<10} | r@1   | r@5   | r@10  | mrr   | hit@1 | hit@5 "
    print(head)
    print("-" * len(head))
    for gkey, label, _ in GROUPS:
        for i, kb in enumerate(KBS):
            m = results[gkey].get(kb, {})
            row = (
                f"{label if i == 0 else '':<32} | {kb:<10} | "
                f"{fmt(m, 'recall@1')} | {fmt(m, 'recall@5')} | {fmt(m, 'recall@10')} | "
                f"{fmt(m, 'mrr')} | {fmt(m, 'hit@1')} | {fmt(m, 'hit@5')}"
            )
            print(row)
        if gkey != GROUPS[-1][0]:
            print("-" * len(head))


def compute_deltas(results: dict) -> dict:
    """以组 1 (vector+noctx) 为基线，计算每组相对提升。"""
    deltas: dict = {}
    base = results["1_vector_noctx"]
    for gkey, label, _ in GROUPS[1:]:
        deltas[gkey] = {}
        for kb in KBS:
            m_cur = results[gkey].get(kb, {})
            m_base = base.get(kb, {})
            deltas[gkey][kb] = {
                k: (m_cur.get(k, 0) - m_base.get(k, 0))
                for k in ("recall@5", "recall@10", "mrr", "hit@1", "hit@5", "ndcg@5")
            }
    return deltas


def write_markdown_report(results: dict, deltas: dict, out_path: Path) -> None:
    lines: list[str] = []
    lines.append("# Phase 8.2.3 评测对比矩阵\n")
    lines.append("> 跑分时间：2026-05-18  |  git: fcac185  |  top_k=10  |  with_generation=False\n")
    lines.append("> 评测集：agv_demo (58 items) + ifs_docs (55 items)\n")

    lines.append("## 一、4 组矩阵\n")
    lines.append("| Group | KB | Recall@1 | Recall@5 | Recall@10 | MRR | Hit@1 | Hit@5 | nDCG@5 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for gkey, label, _ in GROUPS:
        for kb in KBS:
            m = results[gkey].get(kb, {})
            lines.append(
                f"| {label} | {kb} | {fmt(m,'recall@1')} | {fmt(m,'recall@5')} | "
                f"{fmt(m,'recall@10')} | {fmt(m,'mrr')} | {fmt(m,'hit@1')} | "
                f"{fmt(m,'hit@5')} | {fmt(m,'ndcg@5')} |"
            )

    lines.append("\n## 二、相对组 1（vector+noctx）的提升\n")
    lines.append("| Group | KB | ΔRecall@5 | ΔRecall@10 | ΔMRR | ΔHit@1 | ΔnDCG@5 |")
    lines.append("|---|---|---|---|---|---|---|")
    for gkey, label, _ in GROUPS[1:]:
        for kb in KBS:
            d = deltas[gkey][kb]
            def s(v: float) -> str:
                pp = v * 100
                arrow = "↑" if pp > 0 else ("↓" if pp < 0 else " ")
                return f"{arrow}{pp:+.2f}pp"
            lines.append(
                f"| {label} | {kb} | {s(d['recall@5'])} | {s(d['recall@10'])} | "
                f"{s(d['mrr'])} | {s(d['hit@1'])} | {s(d['ndcg@5'])} |"
            )

    lines.append("\n## 三、退出条件判定（PLAN §八）\n")
    lines.append("门槛（agv_demo，从 ifs_docs 已饱和 r@5≈0.99 取信号有限）：")
    lines.append("- Recall@5 提升 ≥10pp **或** MRR 提升 ≥0.05 → 改进有效")
    lines.append("- 若两项均不达标 → 该改进**不上线**\n")

    lines.append("### agv_demo（主要信号 KB）")
    for gkey, label, _ in GROUPS[1:]:
        d = deltas[gkey]["agv_demo"]
        dr5 = d["recall@5"] * 100
        dmrr = d["mrr"]
        win_r5 = dr5 >= 10
        win_mrr = dmrr >= 0.05
        win_any = win_r5 or win_mrr
        verdict = (
            "🟢 显著提升" if win_any
            else ("🟡 持平/微提" if abs(dr5) < 5 and abs(dmrr * 100) < 2.5
                  else "🔴 下降")
        )
        lines.append(
            f"- **{label}**: ΔRecall@5={dr5:+.2f}pp, ΔMRR={dmrr:+.4f} → {verdict}"
        )

    lines.append("\n### ifs_docs（参考信号；评测集饱和）")
    for gkey, label, _ in GROUPS[1:]:
        d = deltas[gkey]["ifs_docs"]
        dr5 = d["recall@5"] * 100
        dmrr = d["mrr"]
        lines.append(f"- {label}: ΔRecall@5={dr5:+.2f}pp, ΔMRR={dmrr:+.4f}")

    lines.append("\n## 四、失败样本对比（agv_demo）\n")
    fail_counts = {}
    for gkey, label, path_tmpl in GROUPS:
        p = EVAL_ROOT / path_tmpl.format(kb="agv_demo")
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                fail_counts[label] = len(json.load(f).get("failures", []))
    for label, n in fail_counts.items():
        lines.append(f"- {label}: {n} failures")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote -> {out_path}")


if __name__ == "__main__":
    results = load_results()
    deltas = compute_deltas(results)
    print_matrix(results)
    write_markdown_report(results, deltas, EVAL_ROOT / "phase8_2_comparison.md")
