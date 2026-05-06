from __future__ import annotations

from typing import Any, Dict, List, Optional


class ToolRegistry:
    """工具注册中心。维护工具名→实例的映射，支持 enabled_tools 过滤。

    规则：
    - final_answer 始终包含在 list_enabled() 结果中（即使调用方未列出）。
    - enabled_tools=None 时返回所有已注册工具。
    """

    _ALWAYS_ENABLED = {"final_answer"}

    def __init__(self) -> None:
        self._tools: Dict[str, Any] = {}

    def register(self, tool: Any) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Any]:
        return self._tools.get(name)

    def list_all(self) -> List[Any]:
        return list(self._tools.values())

    def list_enabled(self, enabled_tools: Optional[List[str]]) -> List[Any]:
        if enabled_tools is None:
            return self.list_all()
        allowed = set(enabled_tools) | self._ALWAYS_ENABLED
        return [t for name, t in self._tools.items() if name in allowed]

    def get_schemas(self, enabled_tools: Optional[List[str]]) -> List[Dict[str, Any]]:
        return [t.openai_schema for t in self.list_enabled(enabled_tools)]
