from __future__ import annotations

from typing import Any, Dict, List

from custom_app.services.google_embedder import embed_query


class KnowledgeSearchTool:
    """语义向量搜索工具，复用 RagRunner 的 FAISS 索引与 rows。"""

    name = "knowledge_search"

    openai_schema: Dict[str, Any] = {
        "type": "function",
        "function": {
            "name": "knowledge_search",
            "description": "在知识库中进行语义向量搜索，返回最相关的文档片段。",
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

    def __init__(self, rows: List[Dict[str, Any]], index: Any) -> None:
        self._rows = rows
        self._index = index

    def run(self, *, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        import numpy as np

        q_vec = embed_query(query).astype("float32").reshape(1, -1)
        k = max(1, min(top_k, len(self._rows)))
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
