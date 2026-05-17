"""Phase 8.1 评测集 schema 定义与校验。

数据格式（JSONL，一行一条）：
    {"id": "eval_001",
     "kb_id": "ifs_docs",
     "query": "如何在 IFS 中查询库存？",
     "relevant_chunk_ids": ["ifs_demo_section_3", "ifs_demo_step_2"],
     "gold_answer": "1. 打开库存模块...\n2. 输入零件号...",
     "tags": ["faq"],
     "source": "session"}

source 字段记录候选来源（不进 PLAN §三.1 必填字段表，但保留便于回溯）：
    - "session"   ← 来自 kb_session_messages
    - "generated" ← Gemini 自动生成后人工筛
    - "manual"    ← 业务侧手写
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

_ALLOWED_SOURCES = {"session", "generated", "manual"}


@dataclass(frozen=True)
class EvalItem:
    """一条评测样本。frozen 保证不可变。"""

    id: str
    kb_id: str
    query: str
    relevant_chunk_ids: tuple[str, ...]
    gold_answer: str
    tags: tuple[str, ...] = ()
    source: str = "manual"

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "EvalItem":
        """从 JSON dict 构造，缺字段或类型不对直接抛 ValueError。"""
        missing = [k for k in ("id", "kb_id", "query", "relevant_chunk_ids", "gold_answer") if k not in row]
        if missing:
            raise ValueError(f"missing required fields: {missing}")

        rid = row["id"]
        if not isinstance(rid, str) or not rid:
            raise ValueError(f"id must be non-empty str, got: {rid!r}")
        kb = row["kb_id"]
        if not isinstance(kb, str) or not kb:
            raise ValueError(f"[{rid}] kb_id must be non-empty str, got: {kb!r}")
        q = row["query"]
        if not isinstance(q, str) or not q.strip():
            raise ValueError(f"[{rid}] query must be non-empty str, got: {q!r}")
        ids = row["relevant_chunk_ids"]
        if not isinstance(ids, list) or not ids or not all(isinstance(x, str) and x for x in ids):
            raise ValueError(f"[{rid}] relevant_chunk_ids must be non-empty list[str], got: {ids!r}")
        gold = row["gold_answer"]
        if not isinstance(gold, str) or not gold.strip():
            raise ValueError(f"[{rid}] gold_answer must be non-empty str, got: {gold!r}")

        tags = row.get("tags", []) or []
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ValueError(f"[{rid}] tags must be list[str], got: {tags!r}")

        source = row.get("source", "manual")
        if source not in _ALLOWED_SOURCES:
            raise ValueError(
                f"[{rid}] source must be one of {_ALLOWED_SOURCES}, got: {source!r}"
            )

        return cls(
            id=rid,
            kb_id=kb,
            query=q.strip(),
            relevant_chunk_ids=tuple(ids),
            gold_answer=gold.strip(),
            tags=tuple(tags),
            source=source,
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "kb_id": self.kb_id,
            "query": self.query,
            "relevant_chunk_ids": list(self.relevant_chunk_ids),
            "gold_answer": self.gold_answer,
        }
        if self.tags:
            d["tags"] = list(self.tags)
        if self.source != "manual":
            d["source"] = self.source
        return d


def validate_unique_ids(items: Iterable[EvalItem]) -> list[str]:
    """返回重复 ID 的清单（空 list 表示无冲突）。"""
    seen: dict[str, int] = {}
    dups: list[str] = []
    for it in items:
        if it.id in seen:
            dups.append(it.id)
        else:
            seen[it.id] = 1
    return dups


def validate_kb_homogeneous(items: Iterable[EvalItem]) -> set[str]:
    """返回集合中出现的所有 kb_id。

    PLAN §八.3 共识：分 KB 报告。但一个 jsonl 文件应只对应一个 KB，便于按文件名归档。
    调用方可断言 len(返回集合) == 1。
    """
    return {it.kb_id for it in items}


@dataclass(frozen=True)
class RetrievalResult:
    """单条样本的检索结果（评测期）。"""

    item_id: str
    retrieved_chunk_ids: tuple[str, ...]  # 按检索得分降序


@dataclass(frozen=True)
class GenerationResult:
    """单条样本的端到端生成结果（仅 --with-generation 时填充）。"""

    item_id: str
    predicted_answer: str


@dataclass(frozen=True)
class EvalReport:
    """单 KB 一次评测的完整结果。"""

    kb_id: str
    n_items: int
    retrieval_metrics: dict[str, float]
    generation_metrics: dict[str, float] = field(default_factory=dict)
    per_tag_retrieval: dict[str, dict[str, float]] = field(default_factory=dict)
    per_tag_generation: dict[str, dict[str, float]] = field(default_factory=dict)
    failures: tuple[dict[str, Any], ...] = ()  # gold 未命中或 F1<0.3 的样本
    run_metadata: dict[str, Any] = field(default_factory=dict)  # 时间戳/git sha/配置

    def to_dict(self) -> dict[str, Any]:
        return {
            "kb_id": self.kb_id,
            "n_items": self.n_items,
            "retrieval_metrics": self.retrieval_metrics,
            "generation_metrics": self.generation_metrics,
            "per_tag_retrieval": self.per_tag_retrieval,
            "per_tag_generation": self.per_tag_generation,
            "failures": list(self.failures),
            "run_metadata": self.run_metadata,
        }
