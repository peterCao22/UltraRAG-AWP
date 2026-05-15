from __future__ import annotations

from typing import Any, Dict, List


class KeywordSearchTool:
    """关键词精确匹配工具，在 title 与 contents 中做大小写不敏感搜索。"""

    name = "keyword_search"

    openai_schema: Dict[str, Any] = {
        "type": "function",
        "function": {
            "name": "keyword_search",
            "description": (
                "在知识库中进行关键词精确匹配搜索，适合查找特定术语或型号。"
                "【幂等】同一 keywords 多次调用结果完全一致，禁止以相同参数重复调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "string",
                        "description": "要搜索的关键词",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回片段数量，默认 5",
                        "default": 5,
                    },
                },
                "required": ["keywords"],
            },
        },
    }

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows

    def run(self, *, keywords: str, top_k: int = 5) -> List[Dict[str, Any]]:
        kw = (keywords or "").strip().lower()
        if not kw:
            return []
        results = []
        for row in self._rows:
            title = (row.get("title") or "").lower()
            contents = (row.get("contents") or "").lower()
            if kw in title or kw in contents:
                results.append({
                    "id": row.get("id", ""),
                    "title": row.get("title", ""),
                    "contents": row.get("contents", ""),
                    "doc": row.get("doc", ""),
                })
            if len(results) >= top_k:
                break
        return results
