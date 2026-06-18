"""P-Perm Commit 1: DB schema 单元测试。

覆盖：
  1. users / user_roles 表存在并含预期列
  2. kb_sessions.user_id 列存在
  3. audit_logs.user_id 列存在
  4. SQLite init_db 幂等：跑两次不报错
  5. users 唯一约束（username UNIQUE）
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch) -> Path:
    """提供一个全新 SQLite，init_db() 跑过一次。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    from custom_app.db import init_db
    init_db()
    return tmp_path / "db" / "app.sqlite"


def _table_columns(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cur.fetchall()}
    finally:
        conn.close()


def test_users_table_created(fresh_db: Path) -> None:
    cols = _table_columns(fresh_db, "users")
    assert {
        "user_id", "username", "password_hash", "display_name",
        "status", "created_at", "updated_at", "last_login_at",
    } <= cols


def test_user_roles_table_created(fresh_db: Path) -> None:
    cols = _table_columns(fresh_db, "user_roles")
    assert {"id", "user_id", "role_id", "created_at"} <= cols


def test_kb_sessions_has_user_id(fresh_db: Path) -> None:
    cols = _table_columns(fresh_db, "kb_sessions")
    assert "user_id" in cols


def test_audit_logs_has_user_id(fresh_db: Path) -> None:
    cols = _table_columns(fresh_db, "audit_logs")
    assert "user_id" in cols


def test_users_username_unique(fresh_db: Path) -> None:
    conn = sqlite3.connect(str(fresh_db))
    try:
        conn.execute(
            "INSERT INTO users (user_id, username, password_hash, created_at, updated_at) "
            "VALUES ('u1', 'alice', 'h1', 't', 't')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO users (user_id, username, password_hash, created_at, updated_at) "
                "VALUES ('u2', 'alice', 'h2', 't', 't')"
            )
    finally:
        conn.close()


def test_user_roles_unique_pair(fresh_db: Path) -> None:
    """(user_id, role_id) UNIQUE：同一用户不能被绑同一角色两次。"""
    conn = sqlite3.connect(str(fresh_db))
    try:
        conn.execute(
            "INSERT INTO user_roles (user_id, role_id, created_at) "
            "VALUES ('u1', 'r1', 't')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO user_roles (user_id, role_id, created_at) "
                "VALUES ('u1', 'r1', 't')"
            )
    finally:
        conn.close()


def test_init_db_idempotent(tmp_path, monkeypatch) -> None:
    """init_db 跑两次不报错（CREATE TABLE IF NOT EXISTS + ALTER 跳过）。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    from custom_app.db import init_db
    init_db()
    init_db()  # 第二次应静默通过


def test_alter_upgrade_old_kb_sessions(tmp_path, monkeypatch) -> None:
    """模拟老库：建一个没有 user_id 的 kb_sessions 表，init_db 后该列被加上。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    db_path = tmp_path / "db" / "app.sqlite"

    # 建一个仅含 Phase 12.2 之前列的旧 kb_sessions
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE kb_sessions ("
        "  session_id TEXT NOT NULL PRIMARY KEY,"
        "  kb_id TEXT NOT NULL,"
        "  title TEXT NOT NULL DEFAULT '',"
        "  agent_mode TEXT NOT NULL DEFAULT 'quick',"
        "  created_at TEXT NOT NULL,"
        "  updated_at TEXT NOT NULL"
        ")"
    )
    conn.commit()
    conn.close()

    from custom_app.db import init_db
    init_db()

    cols = _table_columns(db_path, "kb_sessions")
    # P-Perm 应加 user_id
    assert "user_id" in cols
    # Phase 12.2 也应补齐（ALTER 路径）
    assert "summary" in cols
