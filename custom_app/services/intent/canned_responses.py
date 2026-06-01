"""Phase 11.1.5 模板回复表（zero-token / 一致体验）。

按意图返回一段写死的中文回复；用户在 backlog 选了"写死模板"以减 token、避免 LLM
在非业务问题上瞎扯。

每条模板的设计意图：
    - chitchat:    礼貌 + 引导到业务问题
    - help:        简明能力清单，让用户知道该问什么
    - data_query:  明确说"开发中"，避免误导
"""

from __future__ import annotations

CHITCHAT_RESPONSE = (
    "你好。我是 AGV / SOP 问答助手，可以帮你查 AGV 操作流程、"
    "告警处理步骤、设备说明等。请直接告诉我你遇到的问题，"
    "例如「Alarm 16 怎么处理」或者「换电池的步骤」。"
)

HELP_RESPONSE = (
    "我可以帮你解答以下类型的问题：\n\n"
    "1. **AGV / SOP 操作流程**：换电池、启停流程、维护步骤等\n"
    "2. **告警处理**：Alarm ID、故障描述、恢复步骤\n"
    "3. **设备 / 模块说明**：FTC、UDC、PLS 等模块的功能与配置\n\n"
    "提问技巧：\n"
    "- 直接说告警 ID 或英文名称（如「Alarm Block Battery Low」）效果更好\n"
    "- 如果问题模糊，我会反问澄清\n"
    "- 答案末尾会附上引用的 SOP 文档"
)

DATA_QUERY_RESPONSE = (
    "这个问题需要查询业务系统（如 IFS / MES）的实时数据。"
    "**该功能正在开发中**，目前我只能回答 SOP / 知识类问题。\n\n"
    "如果你想了解相关流程或处理方法（不依赖具体数据），我可以帮你查 SOP。"
)


# 意图 → 模板回复 映射；未在表中的意图返回 None（调用方继续走 RAG）
_RESPONSES: dict[str, str] = {
    "chitchat": CHITCHAT_RESPONSE,
    "help": HELP_RESPONSE,
    "data_query": DATA_QUERY_RESPONSE,
}


def get_canned_response(intent: str) -> str | None:
    """按意图取模板回复；不在表中（如 knowledge）返回 None。"""
    return _RESPONSES.get(intent)
