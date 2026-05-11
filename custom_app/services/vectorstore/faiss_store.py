"""FaissVectorStore —— 包装现有 FAISS 索引为 VectorStore Protocol 实现。

设计选择：
    - 内部仍按"行号 i"工作（IndexIDMap2 的特性），构造时接收 chunk_ids 列表
      做行号 → chunk_id 的映射
    - search() 返回 List[Hit]，Hit.chunk_id 是业务 ID（rag_runner 后续改用 id 检索 _rows）
    - upsert / delete 在 Phase 4 不实现（rebuild 索引由 retriever 服务负责）；
      Phase 5 切 Qdrant 时再补
    - filter 参数 FAISS 忽略（无原生 payload 过滤）；Phase 5 Qdrant 实现

线程安全：
    faiss.read_index 加载后只读取，不并发修改 → 多线程检索是安全的。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

from custom_app.services.vectorstore.base import Hit


class FaissVectorStore:
    """VectorStore 的 FAISS 实现。

    通过 FaissVectorStore.load(index_path, chunk_ids) 构造，
    chunk_ids 顺序必须与 chunks.jsonl 一致（行号 i ↔ chunk_ids[i]）。
    """

    def __init__(self, index: object, chunk_ids: list[str]) -> None:
        self._index = index
        self._chunk_ids = list(chunk_ids)

    @classmethod
    def load(cls, index_path: Path, chunk_ids: list[str]) -> "FaissVectorStore":
        """从持久化文件加载索引。

        Args:
            index_path: .index 文件路径
            chunk_ids:  与索引行号对应的 chunk_id 列表（顺序敏感）

        Raises:
            FileNotFoundError: 索引文件不存在
            ImportError:       faiss 包未安装
        """
        import faiss  # type: ignore

        if not index_path.exists():
            raise FileNotFoundError(f"faiss index not found: {index_path}")
        index = faiss.read_index(str(index_path))
        if index.ntotal != len(chunk_ids):
            raise ValueError(
                f"index size {index.ntotal} mismatches chunk_ids length {len(chunk_ids)}"
            )
        return cls(index, chunk_ids)

    def search(
        self,
        query_vector: np.ndarray,
        top_k: int,
        filter: Optional[dict] = None,
    ) -> list[Hit]:
        """FAISS 向量检索。filter 参数在 FAISS 实现下被忽略（仅 Qdrant 实现支持）。"""
        if filter is not None:
            # 不抛异常，让上层代码可以在切换 Qdrant 前先传 filter 占位
            pass
        if top_k <= 0:
            return []
        q = query_vector.astype("float32")
        if q.ndim == 1:
            q = q.reshape(1, -1)
        k = min(top_k, self._index.ntotal)
        if k == 0:
            return []
        distances, indices = self._index.search(q, k)
        hits: list[Hit] = []
        for score, row_idx in zip(distances[0].tolist(), indices[0].tolist()):
            i = int(row_idx)
            if i < 0 or i >= len(self._chunk_ids):
                continue
            hits.append(Hit(chunk_id=self._chunk_ids[i], score=float(score)))
        return hits

    def upsert(
        self,
        chunk_ids: list[str],
        vectors: np.ndarray,
        payloads: Optional[list[dict]] = None,
    ) -> None:
        raise NotImplementedError(
            "FaissVectorStore 不支持增量 upsert；重建索引请用 retriever 服务"
        )

    def delete(self, chunk_ids: list[str]) -> None:
        raise NotImplementedError(
            "FaissVectorStore 不支持按 id 删除；重建索引请用 retriever 服务"
        )

    def size(self) -> int:
        return int(self._index.ntotal)

    # ------------------------------------------------------------------
    # 辅助 API：rag_runner 现在按行号工作，迁移期间提供"行号 → chunk_id"映射
    # Phase 4.2/4.3 KB 路由 + 嵌入改造完成后，rag_runner 应彻底切到 chunk_id 索引
    # ------------------------------------------------------------------

    @property
    def chunk_ids(self) -> list[str]:
        """只读视图，便于上层根据行号取 chunk_id。"""
        return list(self._chunk_ids)
