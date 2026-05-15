"""KbRepository —— knowledge_bases 表的所有 SQL 操作。

业务方法（不含 SQL，便于 Postgres 替换）：
    create(...)             —— 创建 KB
    get(kb_id)              —— 取单条
    list_paginated(...)     —— 分页列表（含 role_id 过滤、status 过滤、doc_count 派生字段）
    update_basic(...)       —— 改 name / description
    mark_indexed(kb_id, ts) —— 更新 last_indexed_at
    archive(kb_id) / hard_delete(kb_id) —— 软删 / 硬删
    exists(kb_id)           —— 存在性检查（用于创建去重）
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


class KbRepository:
    def __init__(self, provider: Optional[ConnectionProvider] = None) -> None:
        self._provider = provider or get_default_provider()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def exists(self, kb_id: str) -> bool:
        """检查 kb_id 是否被活跃（非 archived）的知识库占用。

        archived 的知识库视为已删除，其 kb_id 可被重新创建。
        """
        sql = "SELECT 1 FROM knowledge_bases WHERE kb_id = ? AND status != 'archived'"
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (kb_id,))
            return cur.fetchone() is not None

    def create(
        self,
        *,
        kb_id: str,
        name: str,
        description: str,
        tenant_id: str,
        kb_type: str,
        data_path: str,
        index_path: str,
        embedding_path: str,
        created_at: str,
    ) -> None:
        sql = """INSERT INTO knowledge_bases
                 (kb_id, name, description, tenant_id, status, type, data_path,
                  index_path, embedding_path, created_at, updated_at)
                 VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)"""
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (
                    kb_id, name, description, tenant_id, kb_type,
                    data_path, index_path, embedding_path,
                    created_at, created_at,
                ),
            )

    def get(
        self, kb_id: str, *, include_archived: bool = False
    ) -> Optional[dict[str, Any]]:
        """取单条 KB，带 document_count 派生字段。"""
        status_clause = "" if include_archived else " AND status != 'archived'"
        sql = (
            f"""SELECT kb.*,
                       (SELECT COUNT(*) FROM kb_documents d WHERE d.kb_id = kb.kb_id) AS document_count
                FROM knowledge_bases kb
                WHERE kb.kb_id = ?{status_clause}"""
        )
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (kb_id,))
            return fetch_one_as_dict(cur)

    def get_basic(self, kb_id: str) -> Optional[dict[str, Any]]:
        """取单条 KB 不含 document_count；用于 _kb_type / has_running_job 等内部场景。"""
        sql = "SELECT * FROM knowledge_bases WHERE kb_id = ?"
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (kb_id,))
            return fetch_one_as_dict(cur)

    def list_paginated(
        self,
        *,
        role_id: Optional[str],
        include_archived: bool,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        status_clause = "" if include_archived else " AND kb.status != 'archived'"
        doc_count = (
            "(SELECT COUNT(*) FROM kb_documents d WHERE d.kb_id = kb.kb_id) AS document_count"
        )
        if role_id:
            sql = f"""SELECT kb.kb_id, kb.name, kb.description, kb.tenant_id, kb.status,
                             kb.type, kb.data_path, kb.index_path, kb.embedding_path,
                             kb.last_indexed_at, kb.created_at, kb.updated_at,
                             {doc_count}
                      FROM knowledge_bases kb
                      INNER JOIN role_kb_permissions p ON p.kb_id = kb.kb_id
                      WHERE p.role_id = ?{status_clause}
                      ORDER BY kb.created_at DESC
                      LIMIT ? OFFSET ?"""
            params: tuple = (role_id, limit, offset)
        else:
            sql = f"""SELECT kb.kb_id, kb.name, kb.description, kb.tenant_id, kb.status,
                             kb.type, kb.data_path, kb.index_path, kb.embedding_path,
                             kb.last_indexed_at, kb.created_at, kb.updated_at,
                             {doc_count}
                      FROM knowledge_bases kb
                      WHERE 1=1{status_clause}
                      ORDER BY kb.created_at DESC
                      LIMIT ? OFFSET ?"""
            params = (limit, offset)
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), params)
            return fetch_all_as_dicts(cur)

    def update_basic(
        self, kb_id: str, *, name: str, description: str, updated_at: str
    ) -> None:
        sql = "UPDATE knowledge_bases SET name=?, description=?, updated_at=? WHERE kb_id=?"
        with self._provider.connect() as conn:
            conn.execute(adapt_sql(sql, self._provider), (name, description, updated_at, kb_id))

    def mark_indexed(self, kb_id: str, *, updated_at: str) -> None:
        sql = "UPDATE knowledge_bases SET last_indexed_at=?, updated_at=? WHERE kb_id=?"
        with self._provider.connect() as conn:
            conn.execute(adapt_sql(sql, self._provider), (updated_at, updated_at, kb_id))

    def archive(self, kb_id: str, *, updated_at: str) -> None:
        sql = "UPDATE knowledge_bases SET status='archived', updated_at=? WHERE kb_id=?"
        with self._provider.connect() as conn:
            conn.execute(adapt_sql(sql, self._provider), (updated_at, kb_id))

    def hard_delete(self, kb_id: str) -> None:
        sql = "DELETE FROM knowledge_bases WHERE kb_id = ?"
        with self._provider.connect() as conn:
            conn.execute(adapt_sql(sql, self._provider), (kb_id,))
