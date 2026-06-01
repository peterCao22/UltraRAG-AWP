"""AuditRepository — append-only 问答审计日志（Phase 11.1.2 最小版）。

设计约束：
    - 只提供 INSERT 和读接口；**不暴露 UPDATE / DELETE**
    - 失败由上层 audit_log 服务捕获 + 降级，不让 Repository 抛入主链路

字段约定见 `_POSTGRES_SCHEMA_DDL` audit_logs：
    id, ts, tenant_id, session_id, kb_id, event_type,
    query, answer, chunk_ids(JSON 字符串), meta(JSON 字符串)
"""

from __future__ import annotations

from typing import Any, Optional

from custom_app.repositories.base import (
    ConnectionProvider,
    adapt_sql,
    fetch_all_as_dicts,
    get_default_provider,
)


class AuditRepository:
    """问答审计 — append-only。"""

    def __init__(self, provider: Optional[ConnectionProvider] = None) -> None:
        self._provider = provider or get_default_provider()

    def append(
        self,
        *,
        ts: str,
        event_type: str,
        kb_id: str = "",
        session_id: str = "",
        tenant_id: str = "default",
        query: str = "",
        answer: str = "",
        chunk_ids_json: str = "[]",
        meta_json: str = "{}",
    ) -> None:
        """追加一条审计记录。

        所有字符串字段都已序列化好（chunk_ids/meta 调用方传 JSON 字符串），
        Repository 不做任何业务转换，避免引入隐式格式。
        """
        sql = (
            "INSERT INTO audit_logs "
            "(ts, tenant_id, session_id, kb_id, event_type, query, answer, chunk_ids, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (
                    ts, tenant_id, session_id, kb_id, event_type,
                    query, answer, chunk_ids_json, meta_json,
                ),
            )

    def list_recent(
        self,
        *,
        kb_id: Optional[str] = None,
        session_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """按 ts DESC 分页列出审计记录；上层可按需序列化 chunk_ids/meta。"""
        lim = max(1, min(int(limit), 500))
        off = max(0, int(offset))

        conditions: list[str] = []
        params: list[Any] = []
        if kb_id:
            conditions.append("kb_id = ?")
            params.append(kb_id)
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

        sql = (
            "SELECT id, ts, tenant_id, session_id, kb_id, event_type, "
            "query, answer, chunk_ids, meta "
            "FROM audit_logs" + where + " ORDER BY id DESC LIMIT ? OFFSET ?"
        )
        params.extend([lim, off])
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), tuple(params))
            return fetch_all_as_dicts(cur)

    def count(
        self,
        *,
        kb_id: Optional[str] = None,
        session_id: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> int:
        """按相同过滤条件统计总数（list_recent 分页时用）。"""
        conditions: list[str] = []
        params: list[Any] = []
        if kb_id:
            conditions.append("kb_id = ?")
            params.append(kb_id)
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = "SELECT COUNT(*) AS n FROM audit_logs" + where
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), tuple(params))
            rows = fetch_all_as_dicts(cur)
            if not rows:
                return 0
            return int(rows[0].get("n") or 0)
