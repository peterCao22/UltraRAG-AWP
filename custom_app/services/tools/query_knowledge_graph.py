from __future__ import annotations

import json
from typing import Any, Dict, List

from custom_app.services.kg_search import search_graph


class QueryKnowledgeGraphTool:
    """查询知识库的实体关系图谱，探索跨文档的实体关联。

    当语义搜索和关键词搜索无法发现文档间隐含的关系时，
    通过知识图谱的实体关联发现跨文档的相关 chunk。
    """

    name = "query_knowledge_graph"

    openai_schema: Dict[str, Any] = {
        "type": "function",
        "function": {
            "name": "query_knowledge_graph",
            "description": (
                "查询知识库的实体关系图谱，发现跨文档的实体关联。**强烈推荐**在以下场景调用："
                "1) 用户问 '涉及哪些 X'、'有哪些 X'、'X 之间的关系'、'X 依赖什么'、"
                "'X 配置在哪些表/字段'；"
                "2) 答案需要枚举多个相关实体（如 '库存销售涉及的页签和字段'）；"
                "3) 跨文档对比或追溯关系（如 '出库类型与库存数量字段的关联'）。"
                "输入应是从前面搜索结果中提取的核心实体名称列表（2-5 个），"
                "返回实体的属性、关系链路、和关联的其他文档片段。"
                "查询代价低，凡是稍微涉及多实体关联的问题都应调用一次。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "要查询的实体名称列表（2-5 个）。"
                            "例如：['IFS数据库', '中间层服务']。"
                            "使用简短的关键实体名称。"
                        ),
                    },
                },
                "required": ["entities"],
            },
        },
    }

    def __init__(self, kb_id: str) -> None:
        self.kb_id = kb_id

    def run(self, *, entities: List[str]) -> Dict[str, Any]:
        # 过滤空字符串，限制数量
        clean = [str(e).strip() for e in entities if str(e).strip()]
        if not clean:
            return {"error": "entities 不能为空"}
        clean = clean[:5]

        result = search_graph(self.kb_id, clean)

        if not result["entities"] and not result["neighbor_entities"]:
            return {
                "query_entities": clean,
                "entities": [],
                "relations": [],
                "message": f"在知识图谱中未找到 [{', '.join(clean)}] 相关的实体。尝试使用不同的实体名称，或使用 keyword_search / knowledge_search 工具。",
            }

        # 格式化输出
        output_lines = []

        # 种子实体
        seed_info = []
        for e in result["entities"]:
            chunks = ", ".join(e["chunk_ids"][:3])
            seed_info.append(
                f"- **{e['name']}** ({e['type']}): {e['description'][:80]} "
                f"[chunk: {chunks}]"
            )
        output_lines.append("## 查询到的实体")
        output_lines.extend(seed_info)

        # 关系
        if result["relations"]:
            output_lines.append("\n## 实体关系")
            for r in result["relations"][:10]:
                output_lines.append(
                    f"- **{r['source']}** --[{r['relation_type']}(强度:{r['strength']})]→ **{r['target']}**"
                )
                if r["description"]:
                    output_lines.append(f"  - {r['description']}")

        # 邻居实体
        if result["neighbor_entities"]:
            output_lines.append("\n## 关联发现的实体（跨文档）")
            for e in result["neighbor_entities"][:10]:
                chunks = ", ".join(e["chunk_ids"][:3])
                output_lines.append(
                    f"- **{e['name']}** ({e['type']}): {e['description'][:80]} "
                    f"[chunk: {chunks}]"
                )

        # 关联的 chunk IDs（供后续使用 list_knowledge_chunks 工具）
        chunk_ids = result["all_chunk_ids"]
        if chunk_ids:
            # 提取 doc 前缀去重
            doc_names = list({cid.rsplit("_", 1)[0] for cid in chunk_ids if "_" in cid})
            output_lines.append(f"\n## 关联文档（共 {len(chunk_ids)} 个 chunk）")
            output_lines.append(f"建议用 list_knowledge_chunks 读取: {', '.join(sorted(doc_names)[:8])}")

        return {
            "query_entities": clean,
            "entities": result["entities"],
            "relations": result["relations"],
            "neighbor_entities": result["neighbor_entities"],
            "chunk_count": len(chunk_ids),
            "chunk_ids": chunk_ids,
            "summary": "\n".join(output_lines),
        }
