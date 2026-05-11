"""QdrantVectorStore —— VectorStore Protocol 的 Qdrant 实现。

部署要求：
    - Qdrant 服务（局域网 Docker，默认 http://192.168.8.40:6333）
    - qdrant-client>=1.10：`uv sync --extras storage`

设计要点：
    - collection 命名：<prefix>__<kb_id>（如 custom_app__agv_demo）
    - point id 用 chunk_id（字符串）；FaissVectorStore 用行号，二者通过
      VectorStore Protocol 抽象解耦，RagRunner 切换 backend 无感知
    - payload 字段：
        kb_id        ← 多 KB 隔离的必需过滤键
        doc          ← SOP 扩展、按文档过滤
        source_type  ← 调试 / 路由追踪
        parser       ← 调试 / 路由追踪
        chunk_data   ← 完整 chunk dict（rag_runner 不再依赖 _rows 内存列表）
    - payload 索引：kb_id + doc（其他字段按需后加）
    - 余弦相似度 = Distance.COSINE（与现有 FAISS L2 归一化 + IP 等价）

集合生命周期：
    - 不在 search 时自动创建；建索引时通过 ensure_collection() 显式创建
    - delete_collection() 用于 KB 删除时清理
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import numpy as np

from custom_app.services.vectorstore.base import Hit

logger = logging.getLogger(__name__)


# 默认嵌入维度：与 google_embedder.EMBED_DIM 一致（截断到 768）
DEFAULT_EMBED_DIM = 768


@dataclass(frozen=True)
class QdrantConfig:
    """Qdrant 连接配置（统一从 .env / YAML 注入）。"""

    url: str
    api_key: Optional[str] = None
    collection_prefix: str = "custom_app"
    timeout_sec: int = 30

    @classmethod
    def from_env(cls) -> "QdrantConfig":
        """从环境变量构造（ULTRARAG_QDRANT_* 前缀）。"""
        url = os.environ.get("ULTRARAG_QDRANT_URL", "").strip()
        if not url:
            raise ValueError("ULTRARAG_QDRANT_URL not set in environment")
        api_key = os.environ.get("ULTRARAG_QDRANT_API_KEY", "").strip() or None
        prefix = os.environ.get("ULTRARAG_QDRANT_COLLECTION_PREFIX", "custom_app").strip()
        try:
            timeout = int(os.environ.get("ULTRARAG_QDRANT_TIMEOUT_SEC", "30"))
        except ValueError:
            timeout = 30
        return cls(url=url, api_key=api_key, collection_prefix=prefix, timeout_sec=timeout)


class QdrantVectorStore:
    """VectorStore Protocol 的 Qdrant 实现。

    每个 KB 对应一个 Qdrant collection（命名：<prefix>__<kb_id>）。
    实例化时绑定单个 KB；切 KB 时新建 Store 实例。
    """

    def __init__(
        self,
        kb_id: str,
        config: Optional[QdrantConfig] = None,
        *,
        embed_dim: int = DEFAULT_EMBED_DIM,
    ) -> None:
        self.kb_id = kb_id
        self.config = config or QdrantConfig.from_env()
        self.embed_dim = embed_dim
        self.collection_name = f"{self.config.collection_prefix}__{kb_id}"
        self._client = self._build_client()

    def _build_client(self):
        from qdrant_client import QdrantClient  # type: ignore

        # check_compatibility=False：client 1.18 + server 1.16.2 minor 差 2，
        # 实测核心 API 兼容；后续把 server 升到 1.17+ 后可移除此项
        return QdrantClient(
            url=self.config.url,
            api_key=self.config.api_key,
            timeout=self.config.timeout_sec,
            check_compatibility=False,
        )

    # ------------------------------------------------------------------
    # 集合管理
    # ------------------------------------------------------------------

    def ensure_collection(self, *, recreate: bool = False) -> None:
        """确保 collection 存在；recreate=True 时先删后建。

        建立 payload 索引（kb_id, doc）以支持高效过滤。
        """
        from qdrant_client.http import models as qm  # type: ignore

        exists = self._client.collection_exists(self.collection_name)
        if exists and recreate:
            logger.info("recreating qdrant collection %s", self.collection_name)
            self._client.delete_collection(self.collection_name)
            exists = False

        if not exists:
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=qm.VectorParams(
                    size=self.embed_dim,
                    distance=qm.Distance.COSINE,
                ),
            )
            # 建 payload 索引（kb_id + doc）
            self._client.create_payload_index(
                collection_name=self.collection_name,
                field_name="kb_id",
                field_schema=qm.PayloadSchemaType.KEYWORD,
            )
            self._client.create_payload_index(
                collection_name=self.collection_name,
                field_name="doc",
                field_schema=qm.PayloadSchemaType.KEYWORD,
            )
            logger.info("created qdrant collection %s (dim=%s)", self.collection_name, self.embed_dim)

    def delete_collection(self) -> None:
        """删除整个 collection（KB 删除时调用）。"""
        if self._client.collection_exists(self.collection_name):
            self._client.delete_collection(self.collection_name)
            logger.info("deleted qdrant collection %s", self.collection_name)

    # ------------------------------------------------------------------
    # VectorStore Protocol
    # ------------------------------------------------------------------

    def search(
        self,
        query_vector: np.ndarray,
        top_k: int,
        filter: Optional[dict] = None,
    ) -> list[Hit]:
        from qdrant_client.http import models as qm  # type: ignore

        if top_k <= 0:
            return []
        q = query_vector.astype("float32")
        if q.ndim > 1:
            q = q.reshape(-1)

        qdrant_filter = self._build_filter(filter)

        # 必须取 payload 才能拿到业务 chunk_id（point id 是 hash 后的 int）
        results = self._client.query_points(
            collection_name=self.collection_name,
            query=q.tolist(),
            limit=top_k,
            query_filter=qdrant_filter,
            with_payload=["chunk_id"],
        ).points

        hits: list[Hit] = []
        for r in results:
            payload = r.payload or {}
            chunk_id = str(payload.get("chunk_id", r.id))
            hits.append(Hit(chunk_id=chunk_id, score=float(r.score)))
        return hits

    def upsert(
        self,
        chunk_ids: list[str],
        vectors: np.ndarray,
        payloads: Optional[list[dict]] = None,
    ) -> None:
        """批量写入向量 + payload。

        payload 应包含至少 {kb_id, doc, source_type, parser, chunk_data}。
        如果 payloads=None，自动用 {kb_id: self.kb_id} 兜底。
        """
        from qdrant_client.http import models as qm  # type: ignore

        if len(chunk_ids) != vectors.shape[0]:
            raise ValueError(
                f"chunk_ids length {len(chunk_ids)} != vectors rows {vectors.shape[0]}"
            )
        if payloads is None:
            payloads = [{"kb_id": self.kb_id} for _ in chunk_ids]
        elif len(payloads) != len(chunk_ids):
            raise ValueError(
                f"payloads length {len(payloads)} != chunk_ids length {len(chunk_ids)}"
            )

        # 确保每条 payload 都有 kb_id（否则跨 KB 过滤会漏）
        for p in payloads:
            p.setdefault("kb_id", self.kb_id)

        # Qdrant point id 接受字符串或 UUID 或 int；这里直接用 chunk_id（字符串）
        # 但部分 Qdrant 版本仅接受 UUID / unsigned int；做一个安全转换：
        # 用 hash 把任意字符串映射到 64-bit int（碰撞概率极低）
        # 另存一份 chunk_id 到 payload，搜索时返回正确业务 ID
        points = []
        for cid, vec, pay in zip(chunk_ids, vectors, payloads):
            pay = dict(pay)
            pay["chunk_id"] = cid
            points.append(
                qm.PointStruct(
                    id=_string_id_to_point_id(cid),
                    vector=vec.astype("float32").tolist(),
                    payload=pay,
                )
            )

        # 分批 upsert 避免单次 payload 过大
        batch_size = 100
        for i in range(0, len(points), batch_size):
            batch = points[i:i + batch_size]
            self._client.upsert(
                collection_name=self.collection_name,
                points=batch,
                wait=True,
            )

    def delete(self, chunk_ids: list[str]) -> None:
        from qdrant_client.http import models as qm  # type: ignore

        if not chunk_ids:
            return
        ids = [_string_id_to_point_id(cid) for cid in chunk_ids]
        self._client.delete(
            collection_name=self.collection_name,
            points_selector=qm.PointIdsList(points=ids),
            wait=True,
        )

    def size(self) -> int:
        if not self._client.collection_exists(self.collection_name):
            return 0
        info = self._client.get_collection(self.collection_name)
        return int(info.points_count or 0)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _build_filter(self, raw_filter: Optional[dict]):
        """把 Phase 4 抽象 filter dict 转成 Qdrant 原生 Filter。

        约定（Phase 5.1 最小实现）：
            {} 或 None         → 无过滤
            {"doc": "X"}       → doc == X
            {"doc": ["A","B"]} → doc IN [A, B]
        """
        from qdrant_client.http import models as qm  # type: ignore

        if not raw_filter:
            return None
        conditions = []
        for key, val in raw_filter.items():
            if isinstance(val, (list, tuple, set)):
                conditions.append(
                    qm.FieldCondition(key=key, match=qm.MatchAny(any=list(val)))
                )
            else:
                conditions.append(
                    qm.FieldCondition(key=key, match=qm.MatchValue(value=val))
                )
        return qm.Filter(must=conditions)

    # 便利方法：从 chunk_id 反向查 chunk_data（migrate 验证用）
    def get_chunk_data(self, chunk_id: str) -> Optional[dict]:
        from qdrant_client.http import models as qm  # type: ignore

        results = self._client.retrieve(
            collection_name=self.collection_name,
            ids=[_string_id_to_point_id(chunk_id)],
            with_payload=True,
        )
        if not results:
            return None
        return results[0].payload


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _string_id_to_point_id(chunk_id: str) -> int:
    """把任意字符串 chunk_id 稳定映射为 64-bit unsigned int point id。

    Qdrant point id 接受 UUID 或 unsigned int；我们的 chunk_id 是业务字符串
    （如 doc_step_3），用 SHA1 取前 8 字节当 int 用，碰撞概率约 1/2^64。

    上层通过 payload.chunk_id 取回原始字符串 ID。
    """
    import hashlib

    h = hashlib.sha1(chunk_id.encode("utf-8")).digest()
    # 取前 8 字节，big-endian unsigned int
    return int.from_bytes(h[:8], "big", signed=False)
