"""VectorStore Protocol —— Phase 4 向量存储统一接口。

设计原则：
    - 命中返回 chunk_id（业务 ID）而非行号 / point_id，让上层与后端解耦
    - search 返回值带 score（余弦相似度），上层可按需做阈值过滤
    - upsert / delete 是 Phase 5 落地 Qdrant 时的接口；Phase 4 FAISS 实现可不支持

Phase 5 计划：
    QdrantVectorStore 实现这个 Protocol，RagRunner 通过依赖注入切换实现，
    业务代码（chunk_id 索引、SOP 扩展逻辑）不动。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

import numpy as np


@dataclass(frozen=True)
class Hit:
    """向量检索命中。

    chunk_id 是业务标识符（chunks.jsonl 中的 id 字段），
    上层用它去 _rows / Qdrant payload 中取 chunk 内容。
    """

    chunk_id: str
    score: float


@runtime_checkable
class VectorStore(Protocol):
    """向量存储 Protocol。

    生命周期：
        1. 构造：传入持久化路径或服务端点
        2. load() 或 connect()：加载索引到可查询状态
        3. search() / upsert() / delete()：业务操作
    """

    def search(
        self,
        query_vector: np.ndarray,
        top_k: int,
        filter: Optional[dict] = None,
    ) -> list[Hit]:
        """向量检索。

        Args:
            query_vector: 形状 (1, D) 或 (D,) 的 float32 向量；实现侧负责 reshape
            top_k:        返回前 K 个最相似命中
            filter:       payload 过滤条件（Phase 5 Qdrant 支持）；FAISS 实现忽略

        Returns:
            按 score 降序排列的 Hit 列表；空索引时返回空列表。
        """
        ...

    def upsert(
        self,
        chunk_ids: list[str],
        vectors: np.ndarray,
        payloads: Optional[list[dict]] = None,
    ) -> None:
        """批量写入 / 更新向量。

        Args:
            chunk_ids: 业务 ID 列表，长度 N
            vectors:   形状 (N, D) 的 float32 向量数组
            payloads:  payload 字典列表（Phase 5 Qdrant 用）；FAISS 忽略

        Raises:
            NotImplementedError: FAISS 实现不支持增量写入时抛出
        """
        ...

    def delete(self, chunk_ids: list[str]) -> None:
        """按 chunk_id 删除向量。

        FAISS 实现不支持删除时应抛 NotImplementedError。
        """
        ...

    def size(self) -> int:
        """当前索引中的向量数量。"""
        ...


def load_faiss_store(
    index_path: Path,
    chunk_ids: list[str],
) -> "VectorStore":
    """便利函数：加载 FAISS 索引并返回 VectorStore 实例。

    放在 base.py 是为了 rag_runner 不必直接 import 具体实现类型；
    切换 Qdrant 时改这一个函数即可。
    """
    from custom_app.services.vectorstore.faiss_store import FaissVectorStore

    return FaissVectorStore.load(index_path, chunk_ids)
