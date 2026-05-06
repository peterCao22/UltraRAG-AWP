from __future__ import annotations

from typing import Any, Dict


class FinalAnswerTool:
    """终止 ReAct 循环的工具，Agent 调用此工具表示已完成推理，提交最终答案。"""

    name = "final_answer"

    openai_schema: Dict[str, Any] = {
        "type": "function",
        "function": {
            "name": "final_answer",
            "description": "提交最终答案，结束推理循环。当你已收集足够信息可以回答用户问题时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": "完整的最终答案，使用 Markdown 格式",
                    },
                },
                "required": ["answer"],
            },
        },
    }

    def run(self, *, answer: str) -> Dict[str, Any]:
        return {"answer": answer, "stop": True}
