"""Phase 8.1 评测驱动入口。

用法：
    # 只跑检索（默认；省 Gemini 配额；CI 推荐）
    python -m custom_app.scripts.eval_custom_app --kb agv_demo

    # 跑端到端（含生成指标）
    python -m custom_app.scripts.eval_custom_app --kb agv_demo --with-generation

    # 写入 baseline（默认时间戳后缀）
    python -m custom_app.scripts.eval_custom_app --kb agv_demo --save-baseline

输出（控制台）：
    - 检索：Recall@1 / Recall@5 / Recall@10 / Hit@k / MRR / nDCG@k
    - 生成（仅 --with-generation）：avg_acc / avg_f1 / avg_em / avg_coverem / avg_rouge-l ...
    - 分桶：tags 至少 3 条样本才出独立行
    - 失败样本数：top-5 未命中或 F1<0.3

退出码：
    0 — 评测成功，所有数据齐全
    1 — 评测集为空或加载失败
    2 — RagRunner 初始化失败
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from custom_app.services.eval.runner import EvalRunner, write_report

load_dotenv()
_logger = logging.getLogger(__name__)


def _print_report(report) -> None:
    print(f"\n=== Eval Report: kb={report.kb_id} ({report.n_items} items) ===")
    md = report.run_metadata
    print(
        f"timestamp={md['timestamp_utc']}  git={md.get('git_sha') or '-'}"
        f"  top_k={md['top_k']}  with_generation={md['with_generation']}"
    )

    print("\n[ Retrieval ]")
    for k, v in sorted(report.retrieval_metrics.items()):
        print(f"  {k:<12} {v:.4f}")

    if report.generation_metrics:
        print("\n[ Generation ]")
        for k, v in sorted(report.generation_metrics.items()):
            print(f"  {k:<14} {v:.4f}")

    if report.per_tag_retrieval:
        print("\n[ Retrieval per tag ]")
        for tag, m in sorted(report.per_tag_retrieval.items()):
            r5 = m.get("recall@5", 0.0)
            mrr_ = m.get("mrr", 0.0)
            print(f"  {tag:<24} recall@5={r5:.3f}  mrr={mrr_:.3f}")

    if report.failures:
        print(f"\n[ Failures: {len(report.failures)} samples ]")
        for f in report.failures[:5]:
            reasons = ",".join(f.get("failure_reason") or [])
            print(f"  - {f['id']} [{reasons}] {f['query'][:60]}")
        if len(report.failures) > 5:
            print(f"  ... and {len(report.failures) - 5} more")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kb", required=True, help="知识库 ID")
    p.add_argument(
        "--dataset",
        type=Path,
        default=None,
        help="评测集路径（默认 data/eval/<kb>.jsonl）",
    )
    p.add_argument(
        "--with-generation",
        action="store_true",
        help="跑生成指标（耗 Gemini 配额；默认只跑检索）",
    )
    p.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="检索 top-k（默认 10）",
    )
    p.add_argument(
        "--save-baseline",
        action="store_true",
        help="把报告写到 data/eval/baseline/<kb>_<date>.json",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="覆盖默认输出路径（与 --save-baseline 互斥）",
    )
    args = p.parse_args(argv)

    dataset_path = args.dataset or Path("data/eval") / f"{args.kb}.jsonl"
    if not dataset_path.exists():
        _logger.error("dataset not found: %s", dataset_path)
        _logger.error(
            "提示：先跑 extract_eval_queries.py + generate_eval_queries.py，"
            "再人工合并/标注到 %s",
            dataset_path,
        )
        return 1

    runner = EvalRunner(kb_id=args.kb, top_k=args.top_k)
    try:
        n = runner.load_dataset(dataset_path)
    except (ValueError, FileNotFoundError) as e:
        _logger.error("failed to load dataset: %s", e)
        return 1
    _logger.info("loaded %d items from %s", n, dataset_path)

    try:
        report = runner.run(with_generation=args.with_generation)
    except RuntimeError as e:
        _logger.error("eval run failed: %s", e)
        return 2

    _print_report(report)

    if args.output or args.save_baseline:
        if args.output:
            out_path = args.output
        else:
            stamp = datetime.now().strftime("%Y-%m-%d")
            out_path = Path("data/eval/baseline") / f"{args.kb}_{stamp}.json"
        write_report(report, out_path)
        _logger.info("wrote baseline to %s", out_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
