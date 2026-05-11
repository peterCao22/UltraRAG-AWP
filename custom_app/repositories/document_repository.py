"""DocumentRepository —— kb_documents 表的所有 SQL 操作。

业务方法：
    upsert(...)          —— 注册/更新文档（含 ON CONFLICT 处理）
    list_for_kb(...)     —— 分页列表
    get(kb_id, doc_id)   —— 取单条
    delete(kb_id, doc_id)
    mark_all_indexed(kb_id, ts)
    mark_pending_failed(kb_id, error, ts)
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


class DocumentRepository:
    def __init__(self, provider: Optional[ConnectionProvider] = None) -> None:
        self._provider = provider or get_default_provider()

    def upsert(
        self,
        *,
        kb_id: str,
        tenant_id: str,
        doc_id: str,
        file_name: str,
        file_type: str,
        file_path: str,
        channel: str,
        status: str,
        updated_at: str,
    ) -> None:
        """注册或更新文档；ON CONFLICT 时刷新所有可变字段。

        SQLite 3.24+ 和 Postgres 9.5+ 都支持 ON CONFLICT 语法。
        """
        sql = """INSERT INTO kb_documents
                 (kb_id, tenant_id, doc_id, file_name, file_type, file_path,
                  channel, status, error_message, created_at, updated_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)
                 ON CONFLICT(kb_id, doc_id) DO UPDATE SET
                   file_name=excluded.file_name, file_type=excluded.file_type,
                   file_path=excluded.file_path, channel=excluded.channel,
                   status=excluded.status, error_message='',
                   updated_at=excluded.updated_at"""
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (
                    kb_id, tenant_id, doc_id, file_name, file_type, file_path,
                    channel, status, updated_at, updated_at,
                ),
            )

    def list_for_kb(
        self, kb_id: str, *, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        sql = (
            """SELECT kb_id, tenant_id, doc_id, file_name, file_type, file_path,
                      channel, status, error_message, created_at, updated_at
               FROM kb_documents WHERE kb_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?"""
        )
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (kb_id, limit, offset))
            return fetch_all_as_dicts(cur)

    def get(self, kb_id: str, doc_id: str) -> Optional[dict[str, Any]]:
        sql = "SELECT doc_id, file_path FROM kb_documents WHERE kb_id = ? AND doc_id = ?"
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (kb_id, doc_id))
            return fetch_one_as_dict(cur)

    def delete(self, kb_id: str, doc_id: str) -> None:
        sql = "DELETE FROM kb_documents WHERE kb_id = ? AND doc_id = ?"
        with self._provider.connect() as conn:
            conn.execute(adapt_sql(sql, self._provider), (kb_id, doc_id))

    def mark_all_indexed(self, kb_id: str, *, updated_at: str) -> None:
        sql = "UPDATE kb_documents SET status='indexed', error_message='', updated_at=? WHERE kb_id=?"
        with self._provider.connect() as conn:
            conn.execute(adapt_sql(sql, self._provider), (updated_at, kb_id))

    def mark_pending_failed(
        self, kb_id: str, *, error: str, updated_at: str
    ) -> None:
        sql = """UPDATE kb_documents SET status='failed', error_message=?, updated_at=?
                 WHERE kb_id=? AND status='pending'"""
        with self._provider.connect() as conn:
            conn.execute(adapt_sql(sql, self._provider), (error, updated_at, kb_id))
