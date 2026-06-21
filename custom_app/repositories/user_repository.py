"""P-Perm UserRepository — users + user_roles 表。"""

from __future__ import annotations

from typing import Any, Optional

from custom_app.repositories.base import (
    ConnectionProvider,
    adapt_sql,
    fetch_all_as_dicts,
    fetch_one_as_dict,
    get_default_provider,
)


class UserRepository:
    def __init__(self, provider: Optional[ConnectionProvider] = None) -> None:
        self._provider = provider or get_default_provider()

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------

    def find_by_username(self, username: str) -> Optional[dict[str, Any]]:
        sql = (
            "SELECT user_id, username, password_hash, display_name, "
            "status, created_at, updated_at, last_login_at "
            "FROM users WHERE username = ?"
        )
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (username,))
            return fetch_one_as_dict(cur)

    def find_by_id(self, user_id: str) -> Optional[dict[str, Any]]:
        sql = (
            "SELECT user_id, username, password_hash, display_name, "
            "status, created_at, updated_at, last_login_at "
            "FROM users WHERE user_id = ?"
        )
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (user_id,))
            return fetch_one_as_dict(cur)

    def exists_username(self, username: str) -> bool:
        sql = "SELECT user_id FROM users WHERE username = ?"
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (username,))
            return cur.fetchone() is not None

    def create(
        self,
        *,
        user_id: str,
        username: str,
        password_hash: str,
        display_name: str,
        created_at: str,
    ) -> None:
        sql = (
            "INSERT INTO users (user_id, username, password_hash, display_name, "
            "status, created_at, updated_at, last_login_at) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?, '')"
        )
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (user_id, username, password_hash, display_name,
                 created_at, created_at),
            )

    def list_active(self) -> list[dict[str, Any]]:
        sql = (
            "SELECT user_id, username, display_name, status, "
            "created_at, last_login_at "
            "FROM users WHERE status = 'active' ORDER BY username"
        )
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), ())
            return fetch_all_as_dicts(cur)

    def update_password(
        self, user_id: str, *, password_hash: str, updated_at: str,
    ) -> None:
        sql = "UPDATE users SET password_hash = ?, updated_at = ? WHERE user_id = ?"
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (password_hash, updated_at, user_id),
            )

    def update_last_login(self, user_id: str, *, when_iso: str) -> None:
        sql = "UPDATE users SET last_login_at = ?, updated_at = ? WHERE user_id = ?"
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (when_iso, when_iso, user_id),
            )

    def update_status(
        self, user_id: str, *, status: str, updated_at: str,
    ) -> None:
        """status: active / disabled。disabled 后无法登录但保留历史数据。"""
        sql = "UPDATE users SET status = ?, updated_at = ? WHERE user_id = ?"
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (status, updated_at, user_id),
            )

    def delete(self, user_id: str) -> None:
        """硬删除用户 + 解绑所有角色。审计日志的 user_id 保留（合规需要）。"""
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(
                    "DELETE FROM user_roles WHERE user_id = ?", self._provider,
                ),
                (user_id,),
            )
            conn.execute(
                adapt_sql("DELETE FROM users WHERE user_id = ?", self._provider),
                (user_id,),
            )

    # ------------------------------------------------------------------
    # user_roles
    # ------------------------------------------------------------------

    def list_role_ids_for_user(self, user_id: str) -> list[str]:
        sql = "SELECT role_id FROM user_roles WHERE user_id = ?"
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (user_id,))
            rows = fetch_all_as_dicts(cur)
            return [str(r["role_id"]) for r in rows]

    def assign_role(
        self, *, user_id: str, role_id: str, created_at: str,
    ) -> None:
        """绑定 user ↔ role；幂等（IGNORE / ON CONFLICT 兼容两后端）。"""
        backend = getattr(self._provider, "backend_name", "sqlite")
        if backend == "postgres":
            sql = (
                "INSERT INTO user_roles (user_id, role_id, created_at) "
                "VALUES (?, ?, ?) ON CONFLICT (user_id, role_id) DO NOTHING"
            )
        else:
            sql = (
                "INSERT OR IGNORE INTO user_roles (user_id, role_id, created_at) "
                "VALUES (?, ?, ?)"
            )
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider), (user_id, role_id, created_at),
            )

    def revoke_role(self, *, user_id: str, role_id: str) -> bool:
        sql = "DELETE FROM user_roles WHERE user_id = ? AND role_id = ?"
        with self._provider.connect() as conn:
            cur = conn.execute(
                adapt_sql(sql, self._provider), (user_id, role_id),
            )
            return getattr(cur, "rowcount", 0) > 0

    def list_kb_ids_accessible(self, user_id: str) -> list[str]:
        """用户能访问的 KB 列表（按 role 的 role_kb_permissions 集合）。

        实现：JOIN user_roles ↔ role_kb_permissions，去重。
        """
        sql = (
            "SELECT DISTINCT p.kb_id FROM user_roles ur "
            "JOIN role_kb_permissions p ON p.role_id = ur.role_id "
            "WHERE ur.user_id = ?"
        )
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (user_id,))
            rows = fetch_all_as_dicts(cur)
            return [str(r["kb_id"]) for r in rows]

    def user_has_kb_permission(
        self, *, user_id: str, kb_id: str, min_level: str = "read",
    ) -> bool:
        """用户是否有特定 KB 的权限。min_level: read / write / admin。

        access_level 偏序：admin > write > read；min_level=read 时三者都符合。
        """
        levels_satisfying = {
            "read": ("read", "write", "admin"),
            "write": ("write", "admin"),
            "admin": ("admin",),
        }
        accepted = levels_satisfying.get(min_level, ("read", "write", "admin"))
        placeholders = ", ".join(["?"] * len(accepted))
        sql = (
            f"SELECT 1 FROM user_roles ur "
            f"JOIN role_kb_permissions p ON p.role_id = ur.role_id "
            f"WHERE ur.user_id = ? AND p.kb_id = ? "
            f"AND p.access_level IN ({placeholders}) LIMIT 1"
        )
        with self._provider.connect() as conn:
            cur = conn.execute(
                adapt_sql(sql, self._provider),
                (user_id, kb_id, *accepted),
            )
            return cur.fetchone() is not None
