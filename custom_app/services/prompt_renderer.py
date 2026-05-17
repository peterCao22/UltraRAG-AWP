"""Phase 7.2.A: Placeholder 渲染器。

支持的 placeholder（MVP 子集）：
    {{language}}          —— 默认 "Chinese (Simplified)"，可由 context 覆盖
    {{current_time}}      —— 默认 ISO 8601 UTC，可由 context 覆盖
    {{kb_name}}           —— 由 context 显式提供，缺失则保留原样
    {{kb_description}}    —— 同上

未识别的 {{key}} 保留原样，便于将来扩展。
不做 {{contexts}} / {{query}} —— 它们由 RagRunner 内部拼接，不应由用户书写。

设计：用正则 \\{\\{\\s*key\\s*\\}\\} 抓取所有占位符，逐个查 context；
context 优先级高于自动填充值（autofill 字典只用于补缺）。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _autofill_defaults() -> dict[str, str]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "language": "Chinese (Simplified)",
        "current_time": now,
    }


def render_prompt(
    template: str,
    context: Optional[Mapping[str, Any]] = None,
) -> str:
    """把 {{key}} 替换为 context[key]；未识别的 placeholder 保留原样。

    Args:
        template: 含占位符的 prompt 字符串。
        context:  优先生效的字段映射。None 等价于空 dict。

    Returns:
        替换后的 prompt 字符串；不抛异常。

    自动填充（context 未给该 key 时）：
        {{language}}     → "Chinese (Simplified)"
        {{current_time}} → 当前 UTC ISO 时间戳
    """
    if not template:
        return ""

    ctx: dict[str, Any] = dict(_autofill_defaults())
    if context:
        for k, v in context.items():
            if v is None:
                continue
            ctx[str(k)] = v

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in ctx:
            return str(ctx[key])
        return match.group(0)  # 未识别 → 保留 "{{key}}"

    return _PLACEHOLDER_RE.sub(_replace, template)
