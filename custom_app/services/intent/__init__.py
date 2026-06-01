"""Phase 11.1.5 Query 意图分类与短路回复。

入口：
    from custom_app.services.intent import classify_intent, get_canned_response

意图分类（4 类）：
    - chitchat:    闲聊（你好 / 谢谢 / 再见 ...）           → 模板短答，跳 RAG
    - help:        元问题（你能做什么 / 怎么用）          → 模板短答，跳 RAG
    - data_query:  数据查询（本期识别 + 提示开发中）       → 模板短答，跳 RAG
    - knowledge:   知识问答（默认 / 其他）               → 走 RAG
"""

from custom_app.services.intent.classifier import (
    IntentResult,
    classify_intent,
    INTENT_CHITCHAT,
    INTENT_HELP,
    INTENT_DATA_QUERY,
    INTENT_KNOWLEDGE,
)
from custom_app.services.intent.canned_responses import get_canned_response

__all__ = [
    "IntentResult",
    "classify_intent",
    "get_canned_response",
    "INTENT_CHITCHAT",
    "INTENT_HELP",
    "INTENT_DATA_QUERY",
    "INTENT_KNOWLEDGE",
]
