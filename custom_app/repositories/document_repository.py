"""DocumentRepository —— kb_documents 表的所有 SQL 操作。

业务方法：
    upsert(...)                       —— 注册/更新文档（含 ON CONFLICT 处理）
    list_for_kb(...)                  —— 分页列表（含 processed_at / chunk_count）
    get(kb_id, doc_id)                —— 取单条
    delete(kb_id, doc_id)
    mark_all_indexed(kb_id, ts)       —— 全部标 'completed'（兼容旧调用）
    mark_pending_failed(kb_id, ...)   —— 在途文档标 'failed'

Phase 6.1 新增（per-document status tracking）：
    update_document_status(...)       —— 单文档状态原子更新
    batch_get_documents(...)          —— 轮询用，只取指定 doc_ids 的关键字段
    list_documents_with_status(...)   —— 文档列表 API 用，含 summary 派生
    find_stale_processing(...)        —— 启动恢复：查 N 分钟前仍 processing 的行
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


# Phase 6.1: 标准状态枚举（仅作文档参考，DB 不加 CHECK）。
DOC_STATUS_PENDING = "pending"
DOC_STATUS_PARSING = "parsing"
DOC_STATUS_EMBEDDING = "embedding"
DOC_STATUS_INDEXING = "indexing"
DOC_STATUS_COMPLETED = "completed"
DOC_STATUS_FAILED = "failed"
DOC_STATUS_DELETING = "deleting"

# 旧值 -> 新值映射（向后兼容；读出来后由 repo 层归一化）
_LEGACY_STATUS_MAP = {
    "done": DOC_STATUS_COMPLETED,
    "indexed": DOC_STATUS_COMPLETED,
}

# 视为"在途"（轮询继续 / 启动恢复要检查）的状态。
_PROCESSING_STATUSES = (
    DOC_STATUS_PARSING,
    DOC_STATUS_EMBEDDING,
    DOC_STATUS_INDEXING,
    DOC_STATUS_DELETING,
)


def _normalize_status(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """把旧的 'done'/'indexed' 映射成 'completed'。"""
    if row is None:
        return None
    status = row.get("status")
    if status in _LEGACY_STATUS_MAP:
        row["status"] = _LEGACY_STATUS_MAP[status]
    return row


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
        """分页列表。Phase 6.1：返回新增的 processed_at / chunk_count。"""
        sql = (
            """SELECT kb_id, tenant_id, doc_id, file_name, file_type, file_path,
                      channel, status, error_message,
                      processed_at, chunk_count,
                      created_at, updated_at
               FROM kb_documents WHERE kb_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?"""
        )
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (kb_id, limit, offset))
            rows = fetch_all_as_dicts(cur)
        for r in rows:
            _normalize_status(r)
        return rows

    def get(self, kb_id: str, doc_id: str) -> Optional[dict[str, Any]]:
        """取单条文档（含 Phase 6.1 新字段，便于详情面板 / retry 复用）。"""
        sql = (
            """SELECT kb_id, tenant_id, doc_id, file_name, file_type, file_path,
                      channel, status, error_message,
                      processed_at, chunk_count,
                      created_at, updated_at
               FROM kb_documents WHERE kb_id = ? AND doc_id = ?"""
        )
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (kb_id, doc_id))
            return _normalize_status(fetch_one_as_dict(cur))

    def delete(self, kb_id: str, doc_id: str) -> None:
        sql = "DELETE FROM kb_documents WHERE kb_id = ? AND doc_id = ?"
        with self._provider.connect() as conn:
            conn.execute(adapt_sql(sql, self._provider), (kb_id, doc_id))

    def mark_all_indexed(self, kb_id: str, *, updated_at: str) -> None:
        """全部标 'completed'（保留方法名以兼容现有调用方）。

        processed_at 同步写入；chunk_count 由调用方分配后用
        update_document_status 单独覆盖。
        """
        sql = (
            """UPDATE kb_documents SET status='completed', error_message='',
                      processed_at=?, updated_at=? WHERE kb_id=?"""
        )
        with self._provider.connect() as conn:
            conn.execute(adapt_sql(sql, self._provider), (updated_at, updated_at, kb_id))

    def mark_pending_failed(
        self, kb_id: str, *, error: str, updated_at: str
    ) -> None:
        """把所有还在 pending/parsing/embedding/indexing 的文档标 failed。

        Phase 6.1：错误时 in-flight 文档可能位于任一中间态，不再限定 pending。
        """
        sql = (
            """UPDATE kb_documents SET status='failed', error_message=?, updated_at=?
               WHERE kb_id=? AND status IN ('pending','parsing','embedding','indexing')"""
        )
        with self._provider.connect() as conn:
            conn.execute(adapt_sql(sql, self._provider), (error, updated_at, kb_id))

    # ------------------------------------------------------------------
    # Phase 6.1: per-document status tracking
    # ------------------------------------------------------------------

    def update_document_status(
        self,
        kb_id: str,
        doc_id: str,
        *,
        status: str,
        updated_at: str,
        error_message: Optional[str] = None,
        chunk_count: Optional[int] = None,
        processed_at: Optional[str] = None,
    ) -> None:
        """单文档状态原子更新。

        参数:
            status        新状态（pending/parsing/embedding/indexing/completed/failed/deleting）
            updated_at    本次写入时间戳（必填，由调用方决定，便于测试）
            error_message 写 failed 时传入；其它状态置空（''）
            chunk_count   completed 时传入分块数；其它状态不动
            processed_at  显式 ISO 时间戳（completed 时写入；其它状态保持原值）
        """
        sets = ["status=?", "updated_at=?"]
        params: list[Any] = [status, updated_at]

        if status == DOC_STATUS_FAILED:
            sets.append("error_message=?")
            params.append((error_message or "")[:500])
        elif error_message is None:
            # 进入非 failed 状态时清空旧错误（避免 retry 后还显示老错误）
            sets.append("error_message=?")
            params.append("")
        else:
            sets.append("error_message=?")
            params.append(error_message[:500])

        if chunk_count is not None:
            sets.append("chunk_count=?")
            params.append(int(chunk_count))

        if processed_at is not None:
            sets.append("processed_at=?")
            params.append(processed_at)

        params.extend([kb_id, doc_id])
        sql = (
            f"UPDATE kb_documents SET {', '.join(sets)} "
            "WHERE kb_id=? AND doc_id=?"
        )
        with self._provider.connect() as conn:
            conn.execute(adapt_sql(sql, self._provider), tuple(params))

    def batch_get_documents(
        self, kb_id: str, doc_ids: list[str]
    ) -> list[dict[str, Any]]:
        """轮询用：取指定 doc_ids 的最新状态字段。

        返回字段保持最小化以减少传输：doc_id / status / error_message /
        chunk_count / processed_at / updated_at。
        """
        if not doc_ids:
            return []
        placeholders = ",".join(["?"] * len(doc_ids))
        sql = (
            f"""SELECT doc_id, status, error_message, chunk_count,
                       processed_at, updated_at
                FROM kb_documents
                WHERE kb_id=? AND doc_id IN ({placeholders})"""
        )
        with self._provider.connect() as conn:
            cur = conn.execute(
                adapt_sql(sql, self._provider),
                tuple([kb_id, *doc_ids]),
            )
            rows = fetch_all_as_dicts(cur)
        for r in rows:
            _normalize_status(r)
        return rows

    def list_documents_with_status(
        self, kb_id: str
    ) -> dict[str, Any]:
        """详情列表 + summary。

        返回:
            {
              "documents": [ { ...全字段..., status_normalized } ],
              "summary":   { pending:N, parsing:N, ..., failed:N, deleting:N }
            }
        """
        sql = (
            """SELECT kb_id, tenant_id, doc_id, file_name, file_type, file_path,
                      channel, status, error_message,
                      processed_at, chunk_count,
                      created_at, updated_at
               FROM kb_documents WHERE kb_id=? ORDER BY created_at DESC"""
        )
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (kb_id,))
            rows = fetch_all_as_dicts(cur)
        summary = {
            DOC_STATUS_PENDING: 0,
            DOC_STATUS_PARSING: 0,
            DOC_STATUS_EMBEDDING: 0,
            DOC_STATUS_INDEXING: 0,
            DOC_STATUS_COMPLETED: 0,
            DOC_STATUS_FAILED: 0,
            DOC_STATUS_DELETING: 0,
        }
        for r in rows:
            _normalize_status(r)
            status = r.get("status")
            if status in summary:
                summary[status] += 1
        return {"documents": rows, "summary": summary}

    def find_stale_processing(
        self, *, threshold_iso: str
    ) -> list[dict[str, Any]]:
        """找出 updated_at 早于 threshold_iso 且仍在 processing 的文档。

        启动恢复用。返回 doc_id + kb_id 即可，调用方按 kb_id 调
        update_document_status 标 failed。
        """
        placeholders = ",".join(["?"] * len(_PROCESSING_STATUSES))
        sql = (
            f"""SELECT kb_id, doc_id, status, updated_at
                FROM kb_documents
                WHERE status IN ({placeholders}) AND updated_at < ?"""
        )
        with self._provider.connect() as conn:
            cur = conn.execute(
                adapt_sql(sql, self._provider),
                tuple([*_PROCESSING_STATUSES, threshold_iso]),
            )
            return fetch_all_as_dicts(cur)
