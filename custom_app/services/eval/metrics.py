"""Phase 8.1 字符串生成指标 —— 剥离自 UltraRAG ``servers/evaluation/src/evaluation.py``。

剥离动作（PLAN §五.2）：
    - 删除 ``from ultrarag.server import UltraRAG_MCP_Server`` 与 ``app = UltraRAG_MCP_Server(...)``
    - 删除每个函数上的 ``@app.tool()`` 装饰器
    - ``compute_metrics`` 警告改走 ``logging`` 模块
    - 保留 8 个核心指标 + ``normalize_text`` + ``compute_metrics``
    - 排除 TREC 评估器（pytrec_eval 路径），那部分在 Phase 8.2 也用不到

新增（custom_app 专用）：
    - ``retrieval_metrics``：Recall@k / MRR / Hit@1 / nDCG@k，纯字符串集合运算，无外部依赖

任何 metric 函数签名：``(gt_strings: list[str], pred: str) -> float``。
``gt_strings`` 是同一条样本可接受的多个 gold answer（OR 关系）；
当前评测集每条样本只填一个 ``gold_answer``，调用方需用 ``[item.gold_answer]`` 包裹。
"""
from __future__ import annotations

import logging
import math
import re
import string
from collections import Counter
from typing import Callable, Iterable

_logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 文本归一化
# ─────────────────────────────────────────────────────────────────────────────


def normalize_text(text: str) -> str:
    """对文本做评测前归一化：bool 映射 / 下划线还原 / 小写 / 去标点 / 去冠词 / 压缩空白。"""

    def _bool_mapping(s: str) -> str:
        return {"True": "yes", "False": "no"}.get(s, s)

    def _remove_articles(t: str) -> str:
        return re.sub(r"\b(a|an|the)\b", " ", t)

    def _white_space_fix(t: str) -> str:
        return " ".join(t.split())

    def _remove_punc(t: str) -> str:
        # 在英文标点之外，把常见全角标点也压成空格，方便中文 SOP 评测
        exclude = set(string.punctuation + "‘’´`，。；：、！？（）【】「」《》—…")
        return "".join(ch if ch not in exclude else " " for ch in t)

    def _lower(t: str) -> str:
        return t.lower()

    def _replace_underscore(t: str) -> str:
        return t.replace("_", " ")

    for func in (
        _bool_mapping,
        _replace_underscore,
        _lower,
        _remove_punc,
        _remove_articles,
        _white_space_fix,
    ):
        text = func(text)
    return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
# 生成指标
# ─────────────────────────────────────────────────────────────────────────────


def accuracy_score(gt: list[str], pred: str) -> float:
    """gt 中任一字符串是 pred 子串则记 1.0；否则 0.0。"""
    pred_norm = normalize_text(pred)
    if not pred_norm:
        return 0.0
    gt_norm_ls = [normalize_text(g) for g in gt]
    return 1.0 if any(g and g in pred_norm for g in gt_norm_ls) else 0.0


def exact_match_score(gt: list[str], pred: str) -> float:
    """归一化后 pred 与 gt 中任一完全相等记 1.0。"""
    pred_norm = normalize_text(pred)
    gt_norm_ls = [normalize_text(g) for g in gt]
    return 1.0 if any(pred_norm == g for g in gt_norm_ls) else 0.0


def cover_exact_match_score(gt: list[str], pred: str) -> float:
    """gt 中任一答案的所有 token 都出现在 pred 中记 1.0。"""
    pred_norm = normalize_text(pred)
    pred_tokens = pred_norm.split()
    for g in gt:
        gt_tokens = normalize_text(g).split()
        if gt_tokens and all(t in pred_tokens for t in gt_tokens):
            return 1.0
    return 0.0


def string_em_score(gt: list[str], pred: str) -> float:
    """与 EM 类似但返回精确匹配的占比。"""
    if not gt:
        return 0.0
    pred_norm = normalize_text(pred)
    gt_norm_ls = [normalize_text(g) for g in gt]
    match_cnt = sum(1 for g in gt_norm_ls if pred_norm == g)
    return match_cnt / len(gt_norm_ls)


def f1_score(gt: list[str], pred: str) -> float:
    """token-level F1：取 pred 与 gt 中任一答案的最大 F1。"""

    def _calc(gt_str: str, pred_str: str) -> float:
        pred_tokens = normalize_text(pred_str).split()
        gt_tokens = normalize_text(gt_str).split()
        if not pred_tokens or not gt_tokens:
            return 0.0
        common = Counter(pred_tokens) & Counter(gt_tokens)
        num_same = sum(common.values())
        if num_same == 0:
            return 0.0
        precision = num_same / len(pred_tokens)
        recall = num_same / len(gt_tokens)
        return 2 * precision * recall / (precision + recall)

    scores = [_calc(g, pred) for g in gt]
    return max(scores) if scores else 0.0


# rouge_score 在某些环境下未安装；用 lazy import + 失败降级
_rouge_scorer = None
_rouge_unavailable = False


def _get_rouge_scorer():
    global _rouge_scorer, _rouge_unavailable
    if _rouge_unavailable:
        return None
    if _rouge_scorer is not None:
        return _rouge_scorer
    try:
        from rouge_score import rouge_scorer  # type: ignore

        _rouge_scorer = rouge_scorer.RougeScorer(
            ["rouge1", "rouge2", "rougeL"],
            use_stemmer=True,
        )
        return _rouge_scorer
    except ImportError:
        _logger.warning(
            "rouge_score not installed; rouge1/rouge2/rouge-l metrics will return 0.0"
        )
        _rouge_unavailable = True
        return None


def _rouge_score(gt: list[str], pred: str, key: str) -> float:
    scorer = _get_rouge_scorer()
    if scorer is None:
        return 0.0
    pred_norm = normalize_text(pred)
    scores = []
    for g in gt:
        gt_norm = normalize_text(g)
        if not gt_norm or not pred_norm:
            scores.append(0.0)
            continue
        scores.append(scorer.score(gt_norm, pred_norm)[key].fmeasure)
    return max(scores) if scores else 0.0


def rouge1_score(gt: list[str], pred: str) -> float:
    return _rouge_score(gt, pred, "rouge1")


def rouge2_score(gt: list[str], pred: str) -> float:
    return _rouge_score(gt, pred, "rouge2")


def rouge_l_score(gt: list[str], pred: str) -> float:
    return _rouge_score(gt, pred, "rougeL")


# 别名：UltraRAG 原 API 命名带连字符，新代码用下划线版本
rougel_score = rouge_l_score


GEN_METRIC_REGISTRY: dict[str, Callable[[list[str], str], float]] = {
    "acc": accuracy_score,
    "em": exact_match_score,
    "stringem": string_em_score,
    "coverem": cover_exact_match_score,
    "f1": f1_score,
    "rouge-1": rouge1_score,
    "rouge-2": rouge2_score,
    "rouge-l": rouge_l_score,
}


def compute_generation_metrics(
    gt_list: list[list[str]],
    pred_list: list[str],
    metrics: list[str] | None = None,
) -> dict[str, float]:
    """对齐 UltraRAG ``compute_metrics``：每条样本算各指标，返回 ``avg_*`` 平均值。"""
    if len(gt_list) != len(pred_list):
        raise ValueError(
            f"gt_list ({len(gt_list)}) and pred_list ({len(pred_list)}) length mismatch"
        )

    if not metrics:
        metrics = list(GEN_METRIC_REGISTRY.keys())
    metrics = [m.lower() for m in metrics]

    valid_metrics = []
    for m in metrics:
        if m in GEN_METRIC_REGISTRY:
            valid_metrics.append(m)
        else:
            _logger.warning(
                "unknown metric %r; available: %s",
                m,
                sorted(GEN_METRIC_REGISTRY.keys()),
            )
    per_metric_scores: dict[str, list[float]] = {m: [] for m in valid_metrics}
    for gt, pred in zip(gt_list, pred_list):
        for m in valid_metrics:
            per_metric_scores[m].append(GEN_METRIC_REGISTRY[m](gt, pred))

    return {
        f"avg_{m}": (sum(s) / len(s) if s else 0.0)
        for m, s in per_metric_scores.items()
    }


# ─────────────────────────────────────────────────────────────────────────────
# 检索指标（custom_app 新增）
# ─────────────────────────────────────────────────────────────────────────────


def recall_at_k(gold: Iterable[str], retrieved: list[str], k: int) -> float:
    """|gold ∩ retrieved[:k]| / |gold|。gold 为空返回 0.0。"""
    gold_set = set(gold)
    if not gold_set:
        return 0.0
    top_k = set(retrieved[:k])
    return len(gold_set & top_k) / len(gold_set)


def hit_at_k(gold: Iterable[str], retrieved: list[str], k: int) -> float:
    """top-k 命中任意 gold 即记 1.0。"""
    gold_set = set(gold)
    if not gold_set:
        return 0.0
    return 1.0 if any(r in gold_set for r in retrieved[:k]) else 0.0


def mrr(gold: Iterable[str], retrieved: list[str]) -> float:
    """第一个命中 gold 的位置倒数（1-based）。整个列表都没命中返回 0.0。"""
    gold_set = set(gold)
    if not gold_set:
        return 0.0
    for idx, r in enumerate(retrieved, start=1):
        if r in gold_set:
            return 1.0 / idx
    return 0.0


def ndcg_at_k(gold: Iterable[str], retrieved: list[str], k: int) -> float:
    """nDCG@k：二值相关性（gold ∈ relevant）。无 gold 返回 0.0。

    DCG@k = Σ_{i=1..k} rel_i / log2(i+1)
    iDCG@k = DCG of the ideal ranking (所有 gold 排前面)
    """
    gold_set = set(gold)
    if not gold_set:
        return 0.0
    dcg = 0.0
    for i, r in enumerate(retrieved[:k], start=1):
        if r in gold_set:
            dcg += 1.0 / math.log2(i + 1)
    # 理想 DCG：min(|gold|, k) 个相关项依次排前
    ideal_hits = min(len(gold_set), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    if idcg == 0:
        return 0.0
    return dcg / idcg


def compute_retrieval_metrics(
    gold_list: list[list[str]],
    retrieved_list: list[list[str]],
    *,
    ks: tuple[int, ...] = (1, 5, 10),
) -> dict[str, float]:
    """批量计算检索指标。返回平均 Recall@k / Hit@k / MRR / nDCG@k。"""
    if len(gold_list) != len(retrieved_list):
        raise ValueError(
            f"gold_list ({len(gold_list)}) and retrieved_list ({len(retrieved_list)}) length mismatch"
        )
    n = len(gold_list)
    if n == 0:
        return {}

    out: dict[str, float] = {}
    for k in ks:
        out[f"recall@{k}"] = sum(
            recall_at_k(g, r, k) for g, r in zip(gold_list, retrieved_list)
        ) / n
        out[f"hit@{k}"] = sum(
            hit_at_k(g, r, k) for g, r in zip(gold_list, retrieved_list)
        ) / n
        out[f"ndcg@{k}"] = sum(
            ndcg_at_k(g, r, k) for g, r in zip(gold_list, retrieved_list)
        ) / n
    out["mrr"] = sum(mrr(g, r) for g, r in zip(gold_list, retrieved_list)) / n
    return out
