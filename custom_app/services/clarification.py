"""Phase 12.3 Clarification（系统主动反问）。

判定 query 是否模糊；模糊时返回一个 ClarificationProposal，包含让用户二选一
的简短文案。**反问与生成并行**：即使触发反问，主流程仍生成一个凑合答案，
让用户既可以点反问选项，也可以看现成答案——务实方案，反问太烦人时直接关。

触发信号（命中任一即触发，保守取并集）：
    1. rerank top_score 低于阈值（默认 0.30，bge-reranker 跨域噪声分数大致 0.0-0.4，
       同域明确命中通常 ≥ 0.7）
    2. 命中跨多个不同 doc（top-N 涉及 ≥ MIN_CROSS_DOCS 个 doc）

反问选项生成：**零 LLM**。从跨域命中的 doc 名直接构造 "X 还是 Y？"。
单触发"低分但命中单 doc"时只能用模板"在 {doc} 范围内能否描述得更具体？"。

设计原则：
    - 不抛异常：任何错误返回 ClarificationProposal(triggered=False)
    - 环境变量可关：``ULTRARAG_CLARIFICATION_ENABLED=0``
    - 阈值环境变量可调：``ULTRARAG_CLARIFICATION_SCORE_THRESHOLD`` 等
    - per-tenant / per-kb 调阈值留给未来 Phase 11.1.5 配合
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


DEFAULT_SCORE_THRESHOLD = 0.30
DEFAULT_TOP_N_FOR_DOCS = 5
DEFAULT_MIN_CROSS_DOCS = 2  # ≥2 个不同 doc 即视为跨域
DEFAULT_MAX_OPTIONS = 3      # 选项数硬上限，避免列出 5 个 doc 让用户更晕


@dataclass
class ClarificationProposal:
    """反问提议：是否触发 + 文案 + 选项 + 触发原因。"""

    triggered: bool = False
    question_text: str = ""              # 给用户看的反问文案
    options: list[str] = field(default_factory=list)   # 让用户选的简短标签
    trigger_reasons: list[str] = field(default_factory=list)  # 调试 / 审计用
    top_score: float = 0.0
    cross_docs: list[str] = field(default_factory=list)

    def to_meta(self) -> dict[str, Any]:
        return {
            "triggered": self.triggered,
            "question_text": self.question_text,
            "options": list(self.options),
            "trigger_reasons": list(self.trigger_reasons),
            "top_score": round(self.top_score, 4),
            "cross_docs": list(self.cross_docs),
        }


# ---------------------------------------------------------------------------
# env 读取
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool = True) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# 工具：提取跨域 doc 名（按命中顺序去重，保留 top-N 内）
# ---------------------------------------------------------------------------


def _extract_top_docs(rows: list[dict[str, Any]], hit_ids: list[int],
                     top_n: int) -> list[str]:
    """从 hit_ids top-N 范围内按顺序去重提取 doc 名。"""
    docs: list[str] = []
    for i in hit_ids[:top_n]:
        if not isinstance(i, int) or i < 0 or i >= len(rows):
            continue
        d = str(rows[i].get("doc") or "").strip()
        if d and d not in docs:
            docs.append(d)
    return docs


def _format_doc_label(doc: str) -> str:
    """把 doc 名缩短成用户友好的标签。例: 'Alarm Block Battery Low SOP' → 'Alarm Block Battery Low'。"""
    s = doc.strip()
    # 去末尾 ' SOP' 后缀（领域文件命名常见）
    for suffix in (" SOP", "_SOP"):
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
            break
    # 去单/多分隔符（_/.）→ 空格，更易读
    s = s.replace("_", " ").strip()
    # 太长截断
    if len(s) > 50:
        s = s[:47] + "…"
    return s or doc


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def propose_clarification(
    *,
    question: str,
    hit_ids: list[int],
    rows: list[dict[str, Any]],
    rerank_meta: dict[str, Any] | None = None,
) -> ClarificationProposal:
    """根据检索结果判断是否反问；不触发时返回 triggered=False 的占位。

    参数:
        question:     当前用户原问 (经指代消解后)
        hit_ids:      rerank 后的命中行号列表（按相关性降序）
        rows:         runner._rows 引用，按 hit_ids 反查 doc 字段
        rerank_meta:  含 rerank_top_score；rerank 未启用时为空 dict

    返回:
        ClarificationProposal；triggered=False 时上层应忽略其他字段
    """
    if not _env_bool("ULTRARAG_CLARIFICATION_ENABLED", default=True):
        return ClarificationProposal(triggered=False, trigger_reasons=["disabled"])

    if not question or not hit_ids:
        return ClarificationProposal(triggered=False, trigger_reasons=["empty_input"])

    score_threshold = _env_float(
        "ULTRARAG_CLARIFICATION_SCORE_THRESHOLD", DEFAULT_SCORE_THRESHOLD,
    )
    top_n_for_docs = max(
        2, _env_int("ULTRARAG_CLARIFICATION_TOP_N_FOR_DOCS", DEFAULT_TOP_N_FOR_DOCS),
    )
    min_cross_docs = max(
        2, _env_int("ULTRARAG_CLARIFICATION_MIN_CROSS_DOCS", DEFAULT_MIN_CROSS_DOCS),
    )
    max_options = max(
        2, _env_int("ULTRARAG_CLARIFICATION_MAX_OPTIONS", DEFAULT_MAX_OPTIONS),
    )

    rerank_meta = rerank_meta or {}
    top_score = float(rerank_meta.get("rerank_top_score") or 0.0)

    # 触发器 1：低分（top_score 低于阈值）
    low_score = (
        bool(rerank_meta.get("rerank_applied"))  # 仅 rerank 实际跑过时判分
        and top_score < score_threshold
    )

    # 触发器 2：跨域（top-N 命中跨多个 doc）
    top_docs = _extract_top_docs(rows, hit_ids, top_n_for_docs)
    cross_domain = len(top_docs) >= min_cross_docs

    triggered = low_score or cross_domain
    if not triggered:
        return ClarificationProposal(
            triggered=False,
            trigger_reasons=[],
            top_score=top_score,
            cross_docs=top_docs,
        )

    reasons: list[str] = []
    if low_score:
        reasons.append(f"low_score:{top_score:.3f}<{score_threshold:.3f}")
    if cross_domain:
        reasons.append(f"cross_domain:{len(top_docs)}docs")

    # 选项：优先跨域 doc 名；如只单 doc 命中（仅低分触发），fallback 用一行模板
    options: list[str] = []
    question_text = ""
    if cross_domain and top_docs:
        labels = [_format_doc_label(d) for d in top_docs[:max_options]]
        options = labels
        if len(labels) == 2:
            question_text = (
                f"问题可能涉及多个主题。你想问的是「{labels[0]}」"
                f"还是「{labels[1]}」？"
            )
        else:
            joined = "、".join(f"「{l}」" for l in labels)
            question_text = f"问题可能涉及多个主题：{joined}。请选择一个。"
    else:
        # 单 doc 命中但分数低：让用户补充信息
        single_doc = top_docs[0] if top_docs else ""
        if single_doc:
            label = _format_doc_label(single_doc)
            options = [label, "其他"]
            question_text = (
                f"在「{label}」范围内匹配较弱，能否描述得更具体？"
                f"或者你想问的是其他主题？"
            )
        else:
            options = ["请补充更多信息"]
            question_text = "问题信息较少，可以提供更多上下文吗？"

    return ClarificationProposal(
        triggered=True,
        question_text=question_text,
        options=options,
        trigger_reasons=reasons,
        top_score=top_score,
        cross_docs=top_docs,
    )
