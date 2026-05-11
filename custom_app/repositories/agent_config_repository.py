"""AgentConfigRepository —— kb_agent_configs 表（每 KB 一条工具启用列表）。"""

from __future__ import annotations

from typing import Optional

from custom_app.repositories.base import (
    ConnectionProvider,
    adapt_sql,
    fetch_one_as_dict,
    get_default_provider,
)


class AgentConfigRepository:
    def __init__(self, provider: Optional[ConnectionProvider] = None) -> None:
        self._provider = provider or get_default_provider()

    def get_enabled_tools_json(self, kb_id: str) -> Optional[str]:
        sql = "SELECT enabled_tools_json FROM kb_agent_configs WHERE kb_id = ?"
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (kb_id,))
            row = fetch_one_as_dict(cur)
            return row["enabled_tools_json"] if row else None

    def upsert(
        self, *, kb_id: str, enabled_tools_json: str, updated_at: str
    ) -> None:
        sql = """INSERT INTO kb_agent_configs (kb_id, enabled_tools_json, created_at, updated_at)
                 VALUES (?, ?, ?, ?)
                 ON CONFLICT(kb_id) DO UPDATE SET
                   enabled_tools_json=excluded.enabled_tools_json,
                   updated_at=excluded.updated_at"""
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (kb_id, enabled_tools_json, updated_at, updated_at),
            )
