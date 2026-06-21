"""P-Perm Commit 6：backfill_perm_user_id 脚本单测。

涵盖：
    - DRY-RUN 不修改
    - --apply 把 user_id='' / NULL 的 session/audit 行归到 admin
    - 已归属的行不被覆盖
    - owner 不存在时退出码 1
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")
    from custom_app.repositories.base import set_default_provider
    set_default_provider(None)
    from custom_app.db import init_db
    init_db()
    from custom_app.repositories.base import get_default_provider
    return get_default_provider()


def _seed_admin() -> str:
    from custom_app.services.auth import create_user
    return create_user(
        username="admin", password="admin123", display_name="管理员",
    )


def _seed_session(db, *, sid: str, kb_id: str, user_id: str = "") -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO kb_sessions "
            "(session_id, kb_id, title, agent_mode, "
            "created_at, updated_at, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, kb_id, "t", "quick", "2026-01-01", "2026-01-01", user_id),
        )


def _seed_audit(db, *, ts: str, user_id: str = "") -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO audit_logs "
            "(ts, tenant_id, session_id, kb_id, event_type, "
            "query, answer, chunk_ids, meta, user_id) "
            "VALUES (?, 'default', '', 'kb_x', 'qa', "
            "'q', 'a', '[]', '{}', ?)",
            (ts, user_id),
        )


def _count_unassigned(db, table: str) -> int:
    with db.connect() as conn:
        cur = conn.execute(
            f"SELECT COUNT(*) FROM {table} "
            f"WHERE user_id IS NULL OR user_id = ''"
        )
        return int(cur.fetchone()[0])


def test_dryrun_does_not_modify(db, capsys, monkeypatch) -> None:
    _seed_admin()
    _seed_session(db, sid="sess_old", kb_id="kb_x", user_id="")
    _seed_audit(db, ts="2026-01-01", user_id="")

    from custom_app.scripts.backfill_perm_user_id import main
    monkeypatch.setattr("sys.argv", ["backfill_perm_user_id"])
    rc = main()

    assert rc == 0
    assert _count_unassigned(db, "kb_sessions") == 1
    assert _count_unassigned(db, "audit_logs") == 1


def test_apply_assigns_owner(db, monkeypatch) -> None:
    admin_uid = _seed_admin()
    _seed_session(db, sid="sess_old1", kb_id="kb_x", user_id="")
    _seed_session(db, sid="sess_old2", kb_id="kb_y", user_id="")
    _seed_audit(db, ts="2026-01-01", user_id="")
    _seed_audit(db, ts="2026-01-02", user_id="")
    # 控制组：已归属，不应被覆盖
    _seed_session(db, sid="sess_alice", kb_id="kb_x", user_id="user_alice")
    _seed_audit(db, ts="2026-01-03", user_id="user_alice")

    from custom_app.scripts.backfill_perm_user_id import main
    monkeypatch.setattr("sys.argv", ["backfill_perm_user_id", "--apply"])
    rc = main()

    assert rc == 0
    assert _count_unassigned(db, "kb_sessions") == 0
    assert _count_unassigned(db, "audit_logs") == 0
    # 验证 owner 是 admin
    with db.connect() as conn:
        cur = conn.execute(
            "SELECT user_id FROM kb_sessions WHERE session_id = ?",
            ("sess_old1",),
        )
        assert str(cur.fetchone()[0]) == admin_uid
        cur = conn.execute(
            "SELECT COUNT(*) FROM kb_sessions WHERE user_id = ?",
            (admin_uid,),
        )
        assert int(cur.fetchone()[0]) == 2
    # alice 行未被覆盖
    with db.connect() as conn:
        cur = conn.execute(
            "SELECT user_id FROM kb_sessions WHERE session_id = ?",
            ("sess_alice",),
        )
        assert str(cur.fetchone()[0]) == "user_alice"


def test_owner_not_found_returns_1(db, monkeypatch) -> None:
    _seed_admin()
    from custom_app.scripts.backfill_perm_user_id import main
    monkeypatch.setattr(
        "sys.argv", ["backfill_perm_user_id", "--apply", "--owner", "ghost"],
    )
    rc = main()
    assert rc == 1


def test_apply_when_already_clean(db, monkeypatch) -> None:
    """没有未归属数据时 apply 仍然返回 0。"""
    _seed_admin()
    from custom_app.scripts.backfill_perm_user_id import main
    monkeypatch.setattr("sys.argv", ["backfill_perm_user_id", "--apply"])
    rc = main()
    assert rc == 0
