"""
按知识库（kb_id）持久化 Agent 启用工具列表。

设计要点：
- final_answer / list_knowledge_chunks 为 REQUIRED_TOOLS，set 时自动补回，永远不可关。
- 未配置的 kb_id 走默认值（启用全部工具），保持 Sprint 8 之前的行为不变。
- 白名单：未知工具名一律忽略，防止注入或拼写错误污染 schema 列表。
"""
from __future__ import annotations

import json
from typing import Iterable, List

from custom_app.db import get_conn, now_iso

# 全部已实现的工具名（与 services/tools/ 下的 name 字段一一对应）
ALL_TOOLS: List[str] = [
    "knowledge_search",
    "keyword_search",
    "list_knowledge_chunks",
    "final_answer",
]

# 强制启用、不可关闭的工具
REQUIRED_TOOLS: List[str] = [
    "list_knowledge_chunks",
    "final_answer",
]


def _normalize(tools: Iterable[str]) -> List[str]:
    """白名单过滤 + 强制项补回 + 去重，保留 ALL_TOOLS 中的原始顺序。"""
    requested = set()
    for t in tools or []:
        if isinstance(t, str) and t in ALL_TOOLS:
            requested.add(t)
    requested.update(REQUIRED_TOOLS)
    # 按 ALL_TOOLS 顺序输出，前端展示更稳定
    return [t for t in ALL_TOOLS if t in requested]


def get_enabled_tools(kb_id: str) -> List[str]:
    """读取某 KB 的启用工具列表；未配置时返回默认值（全部启用）。"""
    kid = (kb_id or "").strip()
    if not kid:
        return list(ALL_TOOLS)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT enabled_tools_json FROM kb_agent_configs WHERE kb_id = ?",
            (kid,),
        ).fetchone()
    if row is None:
        return list(ALL_TOOLS)
    try:
        raw = json.loads(row["enabled_tools_json"] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return list(ALL_TOOLS)
    return _normalize(raw if isinstance(raw, list) else [])


def set_enabled_tools(kb_id: str, tools: Iterable[str]) -> List[str]:
    """覆盖写入某 KB 的启用工具列表，返回规范化后的实际写入值。"""
    kid = (kb_id or "").strip()
    if not kid:
        raise ValueError("kb_id is required")
    normalized = _normalize(tools)
    payload = json.dumps(normalized, ensure_ascii=False)
    ts = now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO kb_agent_configs (kb_id, enabled_tools_json, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(kb_id) DO UPDATE SET
                enabled_tools_json = excluded.enabled_tools_json,
                updated_at = excluded.updated_at
            """,
            (kid, payload, ts, ts),
        )
    return normalized
