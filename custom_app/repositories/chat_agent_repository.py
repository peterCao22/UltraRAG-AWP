"""ChatAgentRepository —— agent_configs 表的所有 SQL 操作（Phase 7.2.A）。

业务方法：
    create(...)                                   —— 新增 agent
    get(agent_id, include_deleted=False)          —— 取单条
    list_active(tenant_id=1, include_disabled=False)
                                                  —— 列表（不含已软删）
    update(agent_id, **fields)                    —— 局部更新
    soft_delete(agent_id)                         —— 软删（builtin 由 API 层拦截）
    get_builtin_quick()                           —— 取 builtin-quick 行
    get_builtin_agent()                           —— 取 builtin-agent 行

注意：
    - is_builtin / enabled 在 SQLite 是 INTEGER(0/1)，在 Postgres 是 BOOLEAN；
      _normalize_row 在读出来时统一归一化成 Python bool。
    - temperature 在 SQLite/Postgres 都是 REAL，无需转换。
    - 与 agent_config_repository.py（kb_agent_configs 工具配置）功能不同，
      故新建 chat_agent_repository.py 避免名字冲突。
"""

from __future__ import annotations

from typing import Any, Optional

from custom_app.repositories.base import (
    ConnectionProvider,
    adapt_sql,
    fetch_all_as_dicts,
    fetch_one_as_dict,
    get_default_provider,
)


_BOOL_COLS = ("is_builtin", "enabled")


def _normalize_row(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    for col in _BOOL_COLS:
        if col in row and row[col] is not None:
            row[col] = bool(row[col])
    return row


class ChatAgentRepository:
    def __init__(self, provider: Optional[ConnectionProvider] = None) -> None:
        self._provider = provider or get_default_provider()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        agent_id: str,
        name: str,
        agent_mode: str,
        created_at: str,
        description: str = "",
        avatar: str = "",
        system_prompt: str = "",
        agent_system_prompt: str = "",
        model_id: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        enabled: bool = True,
        is_builtin: bool = False,
        tenant_id: int = 1,
    ) -> None:
        sql = (
            """INSERT INTO agent_configs
               (agent_id, tenant_id, name, description, avatar, agent_mode,
                is_builtin, system_prompt, agent_system_prompt, model_id,
                temperature, max_tokens, enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        )
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (
                    agent_id, tenant_id, name, description, avatar, agent_mode,
                    bool(is_builtin), system_prompt, agent_system_prompt, model_id,
                    float(temperature), int(max_tokens), bool(enabled),
                    created_at, created_at,
                ),
            )

    def get(
        self, agent_id: str, *, include_deleted: bool = False
    ) -> Optional[dict[str, Any]]:
        clause = "" if include_deleted else " AND deleted_at IS NULL"
        sql = f"SELECT * FROM agent_configs WHERE agent_id = ?{clause}"
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (agent_id,))
            return _normalize_row(fetch_one_as_dict(cur))

    def list_active(
        self,
        *,
        tenant_id: int = 1,
        include_disabled: bool = False,
    ) -> list[dict[str, Any]]:
        if include_disabled:
            sql = (
                "SELECT * FROM agent_configs "
                "WHERE tenant_id = ? AND deleted_at IS NULL "
                "ORDER BY is_builtin DESC, created_at ASC"
            )
            params: tuple = (tenant_id,)
        else:
            sql = (
                "SELECT * FROM agent_configs "
                "WHERE tenant_id = ? AND deleted_at IS NULL AND enabled = ? "
                "ORDER BY is_builtin DESC, created_at ASC"
            )
            params = (tenant_id, True)
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), params)
            rows = fetch_all_as_dicts(cur)
        for r in rows:
            _normalize_row(r)
        return rows

    def update(
        self,
        agent_id: str,
        *,
        updated_at: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        avatar: Optional[str] = None,
        system_prompt: Optional[str] = None,
        agent_system_prompt: Optional[str] = None,
        model_id: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        """局部更新；None 表示"不动该字段"。

        agent_mode / is_builtin / tenant_id 不在此方法更新（业务上不允许）。
        """
        sets: list[str] = ["updated_at=?"]
        params: list[Any] = [updated_at]
        if name is not None:
            sets.append("name=?"); params.append(name)
        if description is not None:
            sets.append("description=?"); params.append(description)
        if avatar is not None:
            sets.append("avatar=?"); params.append(avatar)
        if system_prompt is not None:
            sets.append("system_prompt=?"); params.append(system_prompt)
        if agent_system_prompt is not None:
            sets.append("agent_system_prompt=?"); params.append(agent_system_prompt)
        if model_id is not None:
            sets.append("model_id=?"); params.append(model_id)
        if temperature is not None:
            sets.append("temperature=?"); params.append(float(temperature))
        if max_tokens is not None:
            sets.append("max_tokens=?"); params.append(int(max_tokens))
        if enabled is not None:
            sets.append("enabled=?"); params.append(bool(enabled))

        params.append(agent_id)
        sql = f"UPDATE agent_configs SET {', '.join(sets)} WHERE agent_id=?"
        with self._provider.connect() as conn:
            conn.execute(adapt_sql(sql, self._provider), tuple(params))

    def soft_delete(self, agent_id: str, *, deleted_at: str) -> None:
        sql = "UPDATE agent_configs SET deleted_at=?, updated_at=? WHERE agent_id=?"
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (deleted_at, deleted_at, agent_id),
            )

    # ------------------------------------------------------------------
    # builtin helpers
    # ------------------------------------------------------------------

    def get_builtin_quick(self) -> Optional[dict[str, Any]]:
        return self.get("builtin-quick")

    def get_builtin_agent(self) -> Optional[dict[str, Any]]:
        return self.get("builtin-agent")
