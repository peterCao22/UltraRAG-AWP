"""Phase 8.1.5 评测驱动器。

把 RagRunner 跑出来的检索结果 + （可选）生成答案，用 metrics.py 算出 EvalReport。

调用流程：
    runner = EvalRunner(kb_id="agv_demo")
    runner.load_dataset(Path("data/eval/agv_demo.jsonl"))
    report = runner.run(top_k=10, with_generation=False)
    write_report(report, Path("data/eval/baseline/agv_demo_2026-05-17.json"))

关键约束：
    - 0 行 UltraRAG 依赖
    - 与 rag_runner.RagRunner 解耦：只调它的公开 `init()` / `_prepare_chat_context()` / `chat()` 接口
    - 生成默认关；CI 跑检索指标足够日常监控，本地手动加 with_generation=True 出全部指标
"""
from __future__ import annotations

import logging
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from custom_app.services.eval.dataset import load_eval_dataset
from custom_app.services.eval.metrics import (
    GEN_METRIC_REGISTRY,
    compute_generation_metrics,
    compute_retrieval_metrics,
    f1_score,
)
from custom_app.services.eval.schema import EvalItem, EvalReport
from custom_app.services.rag_runner import RagRunner

_logger = logging.getLogger(__name__)

DEFAULT_KS: tuple[int, ...] = (1, 5, 10)
FAILURE_F1_THRESHOLD = 0.3  # F1 低于此线 + gold 未命中 → 失败样本


class EvalRunner:
    """单 KB 的评测驱动器。

    设计：每次评测一个 KB（PLAN §八.3 共识：分 KB 报告）。
    """

    def __init__(
        self,
        kb_id: str,
        *,
        rag_runner: RagRunner | None = None,
        top_k: int = 10,
    ) -> None:
        self.kb_id = kb_id
        self._rag_runner = rag_runner  # 测试可注入 mock
        self._items: list[EvalItem] = []
        self._top_k = int(top_k)

    # ─────────────────────────────────────────────────────────
    # Setup
    # ─────────────────────────────────────────────────────────

    def load_dataset(self, path: Path) -> int:
        """从 jsonl 加载评测集，返回条数。会校验 kb_id 匹配。"""
        self._items = load_eval_dataset(path, expected_kb_id=self.kb_id)
        return len(self._items)

    def set_items(self, items: list[EvalItem]) -> None:
        """直接喂样本（测试用）。"""
        if any(it.kb_id != self.kb_id for it in items):
            raise ValueError("all items must share runner.kb_id")
        self._items = list(items)

    def _ensure_runner(self) -> RagRunner:
        if self._rag_runner is None:
            self._rag_runner = RagRunner(kb_id=self.kb_id)
            self._rag_runner.init()
        return self._rag_runner

    # ─────────────────────────────────────────────────────────
    # Core evaluation
    # ─────────────────────────────────────────────────────────

    def _retrieve_chunk_ids(self, runner: RagRunner, query: str) -> list[str]:
        """调 RagRunner 的内部 prepare，把 hit_ids 翻成 chunk_id（保持检索得分顺序）。"""
        prep = runner._prepare_chat_context(query, top_k=self._top_k)
        hit_ids = prep.get("hit_ids") or []
        rows = getattr(runner, "_rows", []) or []
        ids: list[str] = []
        for i in hit_ids:
            try:
                cid = rows[i].get("id")
            except (IndexError, AttributeError):
                continue
            if isinstance(cid, str) and cid:
                ids.append(cid)
        return ids

    def _generate(self, runner: RagRunner, query: str) -> str:
        out = runner.chat(query)
        # chat() 返回结构化 dict；用 answer 字段（已是展示 Markdown）作 prediction
        return (out.get("answer") or "").strip()

    def run(
        self,
        *,
        with_generation: bool = False,
        ks: tuple[int, ...] = DEFAULT_KS,
        gen_metrics: list[str] | None = None,
    ) -> EvalReport:
        """跑一轮评测。返回 EvalReport。"""
        if not self._items:
            raise RuntimeError("dataset not loaded; call load_dataset() first")

        runner = self._ensure_runner()
        gold_ids_list: list[list[str]] = []
        retrieved_ids_list: list[list[str]] = []
        gold_answers: list[list[str]] = []
        predicted_answers: list[str] = []
        per_item_records: list[dict[str, Any]] = []

        for it in self._items:
            t0 = time.perf_counter()
            retrieved = self._retrieve_chunk_ids(runner, it.query)
            retrieve_ms = int((time.perf_counter() - t0) * 1000)

            answer = ""
            generate_ms: int | None = None
            if with_generation:
                t1 = time.perf_counter()
                try:
                    answer = self._generate(runner, it.query)
                except Exception as e:  # noqa: BLE001 — 单条样本错误不阻塞全局
                    _logger.warning("generation failed on %s: %s", it.id, e)
                    answer = ""
                generate_ms = int((time.perf_counter() - t1) * 1000)

            gold_ids_list.append(list(it.relevant_chunk_ids))
            retrieved_ids_list.append(retrieved)
            gold_answers.append([it.gold_answer])
            predicted_answers.append(answer)
            per_item_records.append(
                {
                    "id": it.id,
                    "query": it.query,
                    "tags": list(it.tags),
                    "gold_chunk_ids": list(it.relevant_chunk_ids),
                    "retrieved_chunk_ids": retrieved[: max(ks)],
                    "gold_answer": it.gold_answer,
                    "predicted_answer": answer,
                    "retrieve_ms": retrieve_ms,
                    "generate_ms": generate_ms,
                }
            )

        retrieval_metrics = compute_retrieval_metrics(
            gold_ids_list, retrieved_ids_list, ks=ks
        )
        generation_metrics: dict[str, float] = {}
        if with_generation:
            generation_metrics = compute_generation_metrics(
                gold_answers,
                predicted_answers,
                metrics=gen_metrics or list(GEN_METRIC_REGISTRY.keys()),
            )

        per_tag_retrieval, per_tag_generation = self._per_tag_metrics(
            per_item_records,
            ks=ks,
            with_generation=with_generation,
            gen_metrics=gen_metrics,
        )

        failures = self._collect_failures(
            per_item_records, ks=ks, with_generation=with_generation
        )

        return EvalReport(
            kb_id=self.kb_id,
            n_items=len(self._items),
            retrieval_metrics=retrieval_metrics,
            generation_metrics=generation_metrics,
            per_tag_retrieval=per_tag_retrieval,
            per_tag_generation=per_tag_generation,
            failures=tuple(failures),
            run_metadata=_collect_run_metadata(
                kb_id=self.kb_id,
                top_k=self._top_k,
                with_generation=with_generation,
                n_items=len(self._items),
            ),
        )

    # ─────────────────────────────────────────────────────────
    # 分桶与失败样本
    # ─────────────────────────────────────────────────────────

    def _per_tag_metrics(
        self,
        records: list[dict[str, Any]],
        *,
        ks: tuple[int, ...],
        with_generation: bool,
        gen_metrics: list[str] | None,
    ) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
        by_tag: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in records:
            for t in r["tags"] or ["_untagged"]:
                by_tag[t].append(r)

        per_tag_retr: dict[str, dict[str, float]] = {}
        per_tag_gen: dict[str, dict[str, float]] = {}
        for tag, recs in by_tag.items():
            if len(recs) < 3:
                # 样本数太少，结论不可靠，跳过
                continue
            golds = [r["gold_chunk_ids"] for r in recs]
            retr = [r["retrieved_chunk_ids"] for r in recs]
            per_tag_retr[tag] = compute_retrieval_metrics(golds, retr, ks=ks)
            if with_generation:
                gas = [[r["gold_answer"]] for r in recs]
                pas = [r["predicted_answer"] for r in recs]
                per_tag_gen[tag] = compute_generation_metrics(
                    gas, pas, metrics=gen_metrics
                )
        return per_tag_retr, per_tag_gen

    def _collect_failures(
        self,
        records: list[dict[str, Any]],
        *,
        ks: tuple[int, ...],
        with_generation: bool,
    ) -> list[dict[str, Any]]:
        """挑出值得人工 review 的失败样本：

        - 检索：top-5 完全没命中任何 gold chunk
        - 生成（仅 with_generation 时）：F1 < FAILURE_F1_THRESHOLD
        """
        top_k_for_recall = max(min(ks), 5) if ks else 5
        out: list[dict[str, Any]] = []
        for r in records:
            gold_set = set(r["gold_chunk_ids"])
            top5 = set(r["retrieved_chunk_ids"][:top_k_for_recall])
            retrieval_miss = not (gold_set & top5)
            gen_low_f1 = False
            f1_val: float | None = None
            if with_generation and r["predicted_answer"]:
                f1_val = f1_score([r["gold_answer"]], r["predicted_answer"])
                gen_low_f1 = f1_val < FAILURE_F1_THRESHOLD
            if retrieval_miss or gen_low_f1:
                out.append(
                    {
                        **r,
                        "failure_reason": [
                            *(["retrieval_miss"] if retrieval_miss else []),
                            *(["gen_low_f1"] if gen_low_f1 else []),
                        ],
                        "f1": f1_val,
                    }
                )
        return out


def _collect_run_metadata(
    *,
    kb_id: str,
    top_k: int,
    with_generation: bool,
    n_items: int,
) -> dict[str, Any]:
    """记录跑分上下文便于回溯（时间戳 / git sha / 配置）。"""
    git_sha: str | None = None
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0:
            git_sha = out.stdout.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": git_sha,
        "kb_id": kb_id,
        "top_k": top_k,
        "with_generation": with_generation,
        "n_items": n_items,
    }


def write_report(report: EvalReport, path: Path) -> None:
    """把报告序列化为 JSON 写文件。"""
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
