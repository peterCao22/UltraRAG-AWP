"""ChatModelRepository —— chat_models 表的所有 SQL 操作（Phase 7）。

业务方法：
    create(...)                    —— 新增对话模型
    get(model_id)                  —— 取单条（含 api_key；service 层负责屏蔽）
    list_active(*, tenant_id=1, include_disabled=False) —— 列表
    update(model_id, **fields)     —— 局部更新；api_key 缺省 / 为 None 时不动
    soft_delete(model_id)          —— 软删（deleted_at = now）
    set_default(model_id)          —— 把该行设为默认，同 tenant 其它清零
    get_default(tenant_id=1)       —— 取默认模型

注意：
    - JSONB 在 Postgres 是原生类型，SQLite 用 TEXT；本 repo 统一存 TEXT(JSON 字符串)，
      调用方 json.loads / json.dumps。
    - is_default / enabled 在 SQLite 是 INTEGER(0/1)，在 Postgres 是 BOOLEAN；
      _row_to_dict 在读出来时统一归一化成 Python bool。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from custom_app.repositories.base import (
    ConnectionProvider,
    adapt_sql,
    fetch_all_as_dicts,
    fetch_one_as_dict,
    get_default_provider,
)


def _normalize_row(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """SQLite 的 INTEGER 0/1 与 Postgres 的 boolean 统一成 Python bool；解 extra_json。"""
    if row is None:
        return None
    if "is_default" in row:
        row["is_default"] = bool(row["is_default"])
    if "enabled" in row:
        row["enabled"] = bool(row["enabled"])
    if "extra_json" in row and isinstance(row["extra_json"], str):
        try:
            row["extra"] = json.loads(row["extra_json"] or "{}")
        except Exception:
            row["extra"] = {}
    return row


class ChatModelRepository:
    def __init__(self, provider: Optional[ConnectionProvider] = None) -> None:
        self._provider = provider or get_default_provider()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        model_id: str,
        name: str,
        provider: str,
        model_name: str,
        base_url: str = "",
        api_key: str = "",
        is_default: bool = False,
        enabled: bool = True,
        description: str = "",
        extra: Optional[dict[str, Any]] = None,
        tenant_id: int = 1,
        created_at: str,
    ) -> None:
        sql = (
            """INSERT INTO chat_models
               (model_id, tenant_id, name, provider, model_name, base_url,
                api_key, is_default, enabled, description, extra_json,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        )
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (
                    model_id, tenant_id, name, provider, model_name,
                    base_url, api_key,
                    bool(is_default), bool(enabled),
                    description,
                    json.dumps(extra or {}, ensure_ascii=False),
                    created_at, created_at,
                ),
            )

    def get(self, model_id: str, *, include_deleted: bool = False) -> Optional[dict[str, Any]]:
        clause = "" if include_deleted else " AND deleted_at IS NULL"
        sql = f"SELECT * FROM chat_models WHERE model_id = ?{clause}"
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (model_id,))
            return _normalize_row(fetch_one_as_dict(cur))

    def list_active(
        self,
        *,
        tenant_id: int = 1,
        include_disabled: bool = False,
    ) -> list[dict[str, Any]]:
        if include_disabled:
            sql = (
                "SELECT * FROM chat_models "
                "WHERE tenant_id = ? AND deleted_at IS NULL "
                "ORDER BY created_at DESC"
            )
            params: tuple = (tenant_id,)
        else:
            sql = (
                "SELECT * FROM chat_models "
                "WHERE tenant_id = ? AND deleted_at IS NULL AND enabled = ? "
                "ORDER BY created_at DESC"
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
        model_id: str,
        *,
        updated_at: str,
        name: Optional[str] = None,
        provider: Optional[str] = None,
        model_name: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        enabled: Optional[bool] = None,
        description: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        """局部更新；None 表示"不动该字段"。

        注意：is_default 不在此处更新；用 set_default(model_id)。
        """
        sets: list[str] = ["updated_at=?"]
        params: list[Any] = [updated_at]
        if name is not None:
            sets.append("name=?"); params.append(name)
        if provider is not None:
            sets.append("provider=?"); params.append(provider)
        if model_name is not None:
            sets.append("model_name=?"); params.append(model_name)
        if base_url is not None:
            sets.append("base_url=?"); params.append(base_url)
        if api_key is not None:
            sets.append("api_key=?"); params.append(api_key)
        if enabled is not None:
            sets.append("enabled=?"); params.append(bool(enabled))
        if description is not None:
            sets.append("description=?"); params.append(description)
        if extra is not None:
            sets.append("extra_json=?")
            params.append(json.dumps(extra, ensure_ascii=False))

        params.append(model_id)
        sql = f"UPDATE chat_models SET {', '.join(sets)} WHERE model_id=?"
        with self._provider.connect() as conn:
            conn.execute(adapt_sql(sql, self._provider), tuple(params))

    def soft_delete(self, model_id: str, *, deleted_at: str) -> None:
        sql = "UPDATE chat_models SET deleted_at=?, updated_at=? WHERE model_id=?"
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (deleted_at, deleted_at, model_id),
            )

    def set_default(self, model_id: str, *, tenant_id: int = 1, updated_at: str) -> None:
        """把该 model 设为默认；同 tenant 内其它清零。两条 UPDATE，非原子但
        最坏情况是出现"没默认"或"两个默认"瞬间——业务上每次都按 ORDER BY
        is_default DESC, created_at ASC 取第一条，前端会立刻刷新，不至于卡死。
        """
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(
                    "UPDATE chat_models SET is_default=?, updated_at=? "
                    "WHERE tenant_id=? AND deleted_at IS NULL",
                    self._provider,
                ),
                (False, updated_at, tenant_id),
            )
            conn.execute(
                adapt_sql(
                    "UPDATE chat_models SET is_default=?, updated_at=? "
                    "WHERE model_id=? AND deleted_at IS NULL",
                    self._provider,
                ),
                (True, updated_at, model_id),
            )

    def get_default(self, *, tenant_id: int = 1) -> Optional[dict[str, Any]]:
        """取默认模型；若没显式默认，按 enabled + 最早创建退化。"""
        sql = (
            "SELECT * FROM chat_models "
            "WHERE tenant_id = ? AND deleted_at IS NULL AND enabled = ? "
            "ORDER BY is_default DESC, created_at ASC LIMIT 1"
        )
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (tenant_id, True))
            return _normalize_row(fetch_one_as_dict(cur))
