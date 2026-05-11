"""SessionRepository —— kb_sessions + kb_session_messages 表。

业务方法：
    create_session(...)
    get_session(session_id)
    list_sessions_for_kb(kb_id)
    update_title(session_id, title, ts)
    update_title_and_mode(session_id, title, mode, ts)
    delete_session(session_id) —— 级联删消息
    append_user_message(...) / append_assistant_message(...)
    list_messages(session_id)
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


class SessionRepository:
    def __init__(self, provider: Optional[ConnectionProvider] = None) -> None:
        self._provider = provider or get_default_provider()

    # ------------------------------------------------------------------
    # kb_sessions
    # ------------------------------------------------------------------

    def create_session(
        self,
        *,
        session_id: str,
        kb_id: str,
        title: str,
        agent_mode: str,
        created_at: str,
    ) -> None:
        sql = """INSERT INTO kb_sessions
                 (session_id, kb_id, title, agent_mode, created_at, updated_at)
                 VALUES (?, ?, ?, ?, ?, ?)"""
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (session_id, kb_id, title, agent_mode, created_at, created_at),
            )

    def get_session(self, session_id: str) -> Optional[dict[str, Any]]:
        sql = (
            "SELECT session_id, kb_id, title, agent_mode, created_at, updated_at "
            "FROM kb_sessions WHERE session_id = ?"
        )
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (session_id,))
            return fetch_one_as_dict(cur)

    def list_sessions_for_kb(self, kb_id: str) -> list[dict[str, Any]]:
        sql = (
            "SELECT session_id, kb_id, title, agent_mode, created_at, updated_at "
            "FROM kb_sessions WHERE kb_id = ? ORDER BY updated_at DESC"
        )
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (kb_id,))
            return fetch_all_as_dicts(cur)

    def update_title(self, session_id: str, *, title: str, updated_at: str) -> None:
        sql = "UPDATE kb_sessions SET title = ?, updated_at = ? WHERE session_id = ?"
        with self._provider.connect() as conn:
            conn.execute(adapt_sql(sql, self._provider), (title[:500], updated_at, session_id))

    def update_title_and_mode(
        self,
        session_id: str,
        *,
        title: str,
        agent_mode: str,
        updated_at: str,
    ) -> None:
        sql = (
            "UPDATE kb_sessions SET title = ?, agent_mode = ?, updated_at = ? "
            "WHERE session_id = ?"
        )
        with self._provider.connect() as conn:
            conn.execute(adapt_sql(sql, self._provider), (title, agent_mode, updated_at, session_id))

    def get_session_kb_and_title(
        self, session_id: str
    ) -> Optional[dict[str, Any]]:
        """同 get_session 但仅返回 kb_id + title（chat.py update title 时用）。"""
        sql = "SELECT kb_id, title FROM kb_sessions WHERE session_id = ?"
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (session_id,))
            return fetch_one_as_dict(cur)

    def delete_session(self, session_id: str) -> bool:
        """级联删除会话和消息；返回是否真的删了。"""
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(
                    "DELETE FROM kb_session_messages WHERE session_id = ?",
                    self._provider,
                ),
                (session_id,),
            )
            cur = conn.execute(
                adapt_sql("DELETE FROM kb_sessions WHERE session_id = ?", self._provider),
                (session_id,),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # kb_session_messages
    # ------------------------------------------------------------------

    def append_user_message(
        self, session_id: str, *, content: str, created_at: str
    ) -> None:
        sql = (
            "INSERT INTO kb_session_messages (session_id, role, content, reasoning_json, created_at) "
            "VALUES (?, 'user', ?, '{}', ?)"
        )
        with self._provider.connect() as conn:
            conn.execute(adapt_sql(sql, self._provider), (session_id, content, created_at))

    def append_assistant_message(
        self,
        session_id: str,
        *,
        content: str,
        reasoning_json: str,
        created_at: str,
    ) -> None:
        sql = (
            "INSERT INTO kb_session_messages (session_id, role, content, reasoning_json, created_at) "
            "VALUES (?, 'assistant', ?, ?, ?)"
        )
        with self._provider.connect() as conn:
            conn.execute(
                adapt_sql(sql, self._provider),
                (session_id, content, reasoning_json, created_at),
            )

    def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        sql = (
            "SELECT id, role, content, reasoning_json, created_at "
            "FROM kb_session_messages WHERE session_id = ? ORDER BY id ASC"
        )
        with self._provider.connect() as conn:
            cur = conn.execute(adapt_sql(sql, self._provider), (session_id,))
            return fetch_all_as_dicts(cur)
