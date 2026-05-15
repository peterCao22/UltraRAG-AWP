from __future__ import annotations

from typing import Any, Dict, List, Optional

from custom_app.services.google_embedder import embed_query


class KnowledgeSearchTool:
    """语义向量搜索工具，支持 FAISS 和 Qdrant 两种后端。

    优先使用 VectorStore（Qdrant / FAISS 统一接口），
    未传入时 fallback 到旧的 FAISS index.search() 调用。
    """

    name = "knowledge_search"

    openai_schema: Dict[str, Any] = {
        "type": "function",
        "function": {
            "name": "knowledge_search",
            "description": (
                "在知识库中进行语义向量搜索，返回最相关的文档片段。"
                "【幂等】同一 query 多次调用结果完全一致，禁止以相同/近义参数重复调用；"
                "若需补充检索，请换不同表述或不同关键词。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索查询词或问题",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回片段数量，默认 5",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    }

    def __init__(
        self,
        rows: List[Dict[str, Any]],
        index: Any = None,
        vector_store: Optional[Any] = None,
    ) -> None:
        self._rows = rows
        self._index = index
        self._vector_store = vector_store
        # chunk_id → 行号映射，VectorStore 路径使用
        self._id_to_row = {
            str(row.get("id", "")): i for i, row in enumerate(rows)
        }

    def run(self, *, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        import numpy as np

        q_vec = embed_query(query).astype("float32").reshape(1, -1)
        k = max(1, min(top_k, len(self._rows)))

        if self._vector_store is not None:
            # VectorStore 路径（支持 Qdrant 和 FaissVectorStore）
            hits = self._vector_store.search(q_vec, k)
            results = []
            for hit in hits:
                row_idx = self._id_to_row.get(hit.chunk_id)
                if row_idx is None:
                    continue
                row = self._rows[row_idx]
                results.append({
                    "id": row.get("id", hit.chunk_id),
                    "title": row.get("title", ""),
                    "contents": row.get("contents", ""),
                    "doc": row.get("doc", ""),
                })
                if len(results) >= top_k:
                    break
        else:
            # 旧 FAISS index 路径（向后兼容 mock 测试）
            _, indices = self._index.search(q_vec, k)
            results = []
            for idx in indices[0].tolist():
                idx = int(idx)
                if idx < 0 or idx >= len(self._rows):
                    continue
                row = self._rows[idx]
                results.append({
                    "id": row.get("id", str(idx)),
                    "title": row.get("title", ""),
                    "contents": row.get("contents", ""),
                    "doc": row.get("doc", ""),
                })
                if len(results) >= top_k:
                    break

        return results
