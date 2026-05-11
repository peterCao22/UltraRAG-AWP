"""RoleRepository —— roles + role_kb_permissions 表。"""

from __future__ import annotations

from typing import Any, Optional

from custom_app.repositories.base import (
    ConnectionProvider,
    adapt_sql,
    fetch_all_as_dicts,
    fetch_one_as_dict,
    get_default_provider,
)


class RoleRepository:
    def __init__(self, provider: Optional[ConnectionProvider] = None) -> None:
        self._provider = provider or get_default_provider()

    # ------------------------------------------------------------------
    # roles
    # ------------------------------------------------------------------

    def find_by_name(self, name: str) -> Optional[dict[str, Any]]:
        sql = "SELECT role_id FROM roles WHERE name = ?"
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (name,))
            return fetch_one_as_dict(cur)

    def find_by_id(self, role_id: str) -> Optional[dict[str, Any]]:
        sql = "SELECT * FROM roles WHERE role_id = ?"
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (role_id,))
            return fetch_one_as_dict(cur)

    def exists(self, role_id: str) -> bool:
        sql = "SELECT role_id FROM roles WHERE role_id = ?"
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (role_id,))
            return cur.fetchone() is not None

    def create(
        self,
        *,
        role_id: str,
        name: str,
        description: str,
        created_at: str,
    ) -> None:
        sql = """INSERT INTO roles (role_id, name, description, created_at, updated_at)
                 VALUES (?, ?, ?, ?, ?)"""
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (role_id, name, description, created_at, created_at),
            )

    def list_all(self) -> list[dict[str, Any]]:
        sql = (
            "SELECT role_id, name, description, created_at, updated_at "
            "FROM roles ORDER BY created_at DESC"
        )
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider))
            return fetch_all_as_dicts(cur)

    def delete(self, role_id: str) -> None:
        """级联删除：先删 permissions，再删 role。"""
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql("DELETE FROM role_kb_permissions WHERE role_id = ?", self._provider),
                (role_id,),
            )
            conn.execute(
                adapt_sql("DELETE FROM roles WHERE role_id = ?", self._provider),
                (role_id,),
            )

    # ------------------------------------------------------------------
    # role_kb_permissions
    # ------------------------------------------------------------------

    def upsert_permission(
        self,
        *,
        role_id: str,
        kb_id: str,
        access_level: str,
        updated_at: str,
    ) -> None:
        sql = """INSERT INTO role_kb_permissions (role_id, kb_id, access_level, created_at, updated_at)
                 VALUES (?, ?, ?, ?, ?)
                 ON CONFLICT(role_id, kb_id) DO UPDATE SET
                   access_level=excluded.access_level, updated_at=excluded.updated_at"""
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (role_id, kb_id, access_level, updated_at, updated_at),
            )

    def list_permissions(self, role_id: str) -> list[dict[str, Any]]:
        """列出某 role 的所有 KB 权限，含 KB 详情。"""
        sql = """SELECT p.kb_id, p.access_level, p.created_at, p.updated_at,
                        k.name AS kb_name, k.status AS kb_status
                 FROM role_kb_permissions p
                 LEFT JOIN knowledge_bases k ON k.kb_id = p.kb_id
                 WHERE p.role_id = ?
                 ORDER BY p.created_at DESC"""
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (role_id,))
            return fetch_all_as_dicts(cur)

    def delete_permission(self, role_id: str, kb_id: str) -> None:
        sql = "DELETE FROM role_kb_permissions WHERE role_id = ? AND kb_id = ?"
        with self._provider.connect() as conn:
            conn.execute(adapt_sql(sql, self._provider), (role_id, kb_id))
