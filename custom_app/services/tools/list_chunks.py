from __future__ import annotations

from typing import Any, Dict, List


class ListChunksTool:
    """Deep Read 工具：按 doc 名称返回该文档的全部 chunk，供 Agent 精读完整 SOP。"""

    name = "list_knowledge_chunks"

    openai_schema: Dict[str, Any] = {
        "type": "function",
        "function": {
            "name": "list_knowledge_chunks",
            "description": (
                "获取指定文档的全部分块内容，用于深度阅读完整步骤。"
                "返回的每个 chunk 含 images 字段（图片相对路径数组），"
                "如答案需要展示该步骤图片，使用 Markdown 语法 ![](/路径) 嵌入。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {
                        "type": "string",
                        "description": "文档名称（如 IFSSOP），从 knowledge_search 结果的 doc 字段获取",
                    },
                },
                "required": ["doc_id"],
            },
        },
    }

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows

    def run(self, *, doc_id: str) -> List[Dict[str, Any]]:
        doc = (doc_id or "").strip()
        if not doc:
            return []
        return [
            {
                "id": row.get("id", ""),
                "title": row.get("title", ""),
                "contents": row.get("contents", ""),
                "doc": row.get("doc", ""),
                "images": list(row.get("images", []) or []),
            }
            for row in self._rows
            if str(row.get("doc", "")).strip() == doc
        ]
