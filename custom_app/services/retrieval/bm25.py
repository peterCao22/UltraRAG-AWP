"""Phase 8.2.2.a —— BM25 关键词召回（jieba 中文分词 + rank_bm25）。

设计：
    - 内存实例，每个 KB 一个 BM25Store
    - 从 RagRunner._rows 直接构建（不读 jsonl 二次）
    - 索引文本：title + heading_path + contents + context（与 embedding 输入对齐）
    - 中英文混合：jieba.cut(cut_all=False) 自动混合分词
    - 返回 Hit(chunk_id, score)，与 VectorStore 输出对齐
    - 失败降级：加载或检索异常 → 返回空列表（PLAN §五.5）

约定：
    BM25 给的是 raw score（非归一化），上层 RRF 融合时按 rank 归一，
    不直接拿 score 与向量 cos 相似度比较。
"""
from __future__ import annotations

import logging
import re
from typing import Iterable, Sequence

from custom_app.services.vectorstore.base import Hit

_logger = logging.getLogger(__name__)

# 把 [IMG: path] 占位行从索引文本剔除，避免图片路径污染 BM25 词典
_IMG_LINE_RE = re.compile(r"^\[IMG:\s*([^\]]+)\]\s*$")

_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _strip_image_placeholders(text: str) -> str:
    if not text:
        return ""
    return "\n".join(
        ln for ln in text.splitlines() if not _IMG_LINE_RE.match(ln.strip())
    ).strip()


def _row_to_index_text(row: dict) -> str:
    """构造一条 chunk 的 BM25 索引文本。

    与 google_embedder.compose_doc_embedding_text 对齐：context > heading_path > title > contents。
    （但不做严格一致：embedder 注重语义，BM25 只关心词面命中。）
    """
    parts: list[str] = []
    context = (row.get("context") or "").strip()
    if context:
        parts.append(context)
    structure = row.get("structure") or {}
    heading_path = structure.get("heading_path") or []
    if heading_path:
        cleaned = [str(h).strip() for h in heading_path if str(h).strip()]
        if cleaned:
            parts.append(" ".join(cleaned))
    title = (row.get("title") or "").strip()
    if title:
        parts.append(title)
    body = _strip_image_placeholders(row.get("contents") or "")
    if body:
        parts.append(body)
    return "\n".join(parts)


def tokenize(text: str) -> list[str]:
    """中英混合分词：jieba 处理中文 + 显式抽英文/数字 token。

    例：'AGV 启动 STEP 3' → ['agv', '启动', 'step', '3', 'AGV', 'STEP']
    （jieba 把英文连读看作一个词；我们额外抽一份小写英文 token 提高匹配率。）
    """
    import jieba

    if not text:
        return []
    tokens: list[str] = []
    for tok in jieba.cut(text, cut_all=False):
        t = tok.strip()
        if t:
            tokens.append(t)
    # 额外拼一份小写英文/数字 token，弥补 jieba 对英文短语切得偏粗
    for m in _ASCII_TOKEN_RE.finditer(text):
        tokens.append(m.group(0).lower())
    return tokens


class BM25Store:
    """单 KB 的 BM25 关键词索引。

    线程不安全（rank_bm25 内部就不是线程安全）；用法是每次 RagRunner.init 时构建一次。
    """

    def __init__(
        self,
        chunk_ids: Sequence[str],
        tokenized_corpus: Sequence[Sequence[str]],
    ) -> None:
        from rank_bm25 import BM25Okapi

        if len(chunk_ids) != len(tokenized_corpus):
            raise ValueError(
                f"chunk_ids ({len(chunk_ids)}) and corpus ({len(tokenized_corpus)}) length mismatch"
            )
        self._chunk_ids = list(chunk_ids)
        # rank_bm25 需要 list[list[str]]
        self._bm25 = BM25Okapi([list(t) for t in tokenized_corpus])

    @classmethod
    def from_rows(cls, rows: Iterable[dict]) -> "BM25Store":
        """从 chunks.jsonl 的行 dict 构造。失败时抛 ValueError。"""
        chunk_ids: list[str] = []
        tokenized: list[list[str]] = []
        for row in rows:
            cid = str(row.get("id") or "").strip()
            if not cid:
                continue
            text = _row_to_index_text(row)
            chunk_ids.append(cid)
            tokenized.append(tokenize(text))
        if not chunk_ids:
            raise ValueError("BM25Store: no valid rows to index")
        return cls(chunk_ids=chunk_ids, tokenized_corpus=tokenized)

    def search(self, query: str, top_k: int) -> list[Hit]:
        """关键词召回前 top_k 个 chunk。

        返回按 score 降序的 Hit 列表；score 为 BM25 raw 分数（非归一化）。
        空 query 或 top_k<=0 返回空列表。
        """
        if not query or top_k <= 0:
            return []
        try:
            tokens = tokenize(query)
            if not tokens:
                return []
            scores = self._bm25.get_scores(tokens)
        except Exception as e:  # noqa: BLE001 — 降级：BM25 内部错误不应阻塞主流程
            _logger.warning("BM25 search failed: %s", e)
            return []

        # 取分数为正的前 top_k；rank_bm25 对所有 chunk 都算分，需自己截断
        scored = [
            (self._chunk_ids[i], float(scores[i]))
            for i in range(len(scores))
            if scores[i] > 0
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        scored = scored[: int(top_k)]
        return [Hit(chunk_id=cid, score=score) for cid, score in scored]

    def size(self) -> int:
        return len(self._chunk_ids)
