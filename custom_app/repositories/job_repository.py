"""JobRepository —— kb_jobs 表的所有 SQL 操作。

业务方法：
    create_ingest_job        —— 入库任务建条
    get(job_id)              —— 取单条
    list_for_kb(...)         —— 分页列表
    has_running(kb_id)       —— 同 KB 是否有进行中任务
    find_running(kb_id)      —— 进行中任务的 job_id 列表（用于 watchdog）
    mark_running / mark_success / mark_failed / mark_stale_recovered
    update_result_json       —— 阶段进度
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


class JobRepository:
    def __init__(self, provider: Optional[ConnectionProvider] = None) -> None:
        self._provider = provider or get_default_provider()

    def create_ingest_job(
        self,
        *,
        job_id: str,
        tenant_id: str,
        kb_id: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> None:
        sql = """INSERT INTO kb_jobs
                 (job_id, tenant_id, kb_id, job_type, status, payload_json, result_json,
                  created_at, updated_at)
                 VALUES (?, ?, ?, 'ingest', 'pending', ?, '{}', ?, ?)"""
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (job_id, tenant_id, kb_id, json.dumps(payload), created_at, created_at),
            )

    def get(self, job_id: str) -> Optional[dict[str, Any]]:
        sql = "SELECT * FROM kb_jobs WHERE job_id = ?"
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (job_id,))
            return fetch_one_as_dict(cur)

    def list_for_kb(
        self, kb_id: str, *, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        sql = (
            """SELECT job_id, tenant_id, kb_id, job_type, status, retry_count, last_error,
                      payload_json, result_json, started_at, finished_at, created_at, updated_at
               FROM kb_jobs WHERE kb_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?"""
        )
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (kb_id, limit, offset))
            return fetch_all_as_dicts(cur)

    def has_running(self, kb_id: str) -> bool:
        sql = "SELECT job_id FROM kb_jobs WHERE kb_id=? AND status='running' LIMIT 1"
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (kb_id,))
            return cur.fetchone() is not None

    def find_running(self, kb_id: str) -> list[dict[str, Any]]:
        sql = "SELECT job_id, started_at FROM kb_jobs WHERE kb_id=? AND status='running'"
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (kb_id,))
            return fetch_all_as_dicts(cur)

    def mark_running(self, job_id: str, *, started_at: str) -> None:
        sql = "UPDATE kb_jobs SET status='running', started_at=?, updated_at=? WHERE job_id=?"
        with self._provider.connect() as conn:
            conn.execute(adapt_sql(sql, self._provider), (started_at, started_at, job_id))

    def mark_success(
        self, job_id: str, *, finished_at: str, result: dict[str, Any]
    ) -> None:
        sql = """UPDATE kb_jobs SET status='success', finished_at=?, result_json=?, updated_at=?
                 WHERE job_id=?"""
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (finished_at, json.dumps(result), finished_at, job_id),
            )

    def mark_failed(
        self, job_id: str, *, finished_at: str, error: str
    ) -> None:
        sql = """UPDATE kb_jobs SET status='failed', finished_at=?, last_error=?, updated_at=?
                 WHERE job_id=?"""
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (finished_at, error, finished_at, job_id),
            )

    def mark_stale_recovered(self, job_id: str, *, finished_at: str) -> None:
        """watchdog：把进程死后残留 running 状态改成 failed。"""
        sql = """UPDATE kb_jobs SET status='failed', finished_at=?,
                 last_error=?, updated_at=? WHERE job_id=? AND status='running'"""
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (finished_at, "stale running job recovered by watchdog", finished_at, job_id),
            )

    def get_result_json(self, job_id: str) -> Optional[str]:
        sql = "SELECT result_json FROM kb_jobs WHERE job_id=?"
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (job_id,))
            row = fetch_one_as_dict(cur)
            return row.get("result_json") if row else None

    def update_result_json(
        self, job_id: str, *, result_json: str, updated_at: str
    ) -> None:
        sql = "UPDATE kb_jobs SET result_json=?, updated_at=? WHERE job_id=?"
        with self._provider.connect() as conn:
            conn.execute(adapt_sql(sql, self._provider), (result_json, updated_at, job_id))
