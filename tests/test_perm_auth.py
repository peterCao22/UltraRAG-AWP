"""P-Perm Commit 2: auth service + UserRepository + 装饰器单元测试。

覆盖：
  - hash_password / verify_password 往返
  - create_user 重名抛错
  - authenticate 成功 / 失败 / 禁用用户
  - login_user / current_user / logout_user
  - UserRepository CRUD + role 绑定 + KB 权限查询
  - @require_user / @require_kb_permission 拦截 + admin token bypass
"""

from __future__ import annotations

import pytest
from flask import Flask, jsonify


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def perm_env(tmp_path, monkeypatch):
    """SQLite 隔离 + init_db。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")
    # 强制 default provider 重置
    from custom_app.repositories.base import set_default_provider
    set_default_provider(None)
    from custom_app.db import init_db
    init_db()
    yield tmp_path
    set_default_provider(None)


@pytest.fixture()
def app(perm_env):
    """最小 Flask app；session 用固定 secret key。"""
    app = Flask("perm-test")
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    return app


# ---------------------------------------------------------------------------
# bcrypt
# ---------------------------------------------------------------------------


def test_hash_password_roundtrip() -> None:
    from custom_app.services.auth import hash_password, verify_password
    h = hash_password("hello-world")
    assert verify_password("hello-world", h) is True
    assert verify_password("wrong", h) is False


def test_hash_password_empty_raises() -> None:
    from custom_app.services.auth import hash_password
    with pytest.raises(ValueError):
        hash_password("")


def test_verify_password_invalid_hash_returns_false() -> None:
    from custom_app.services.auth import verify_password
    assert verify_password("x", "not-a-bcrypt-hash") is False


# ---------------------------------------------------------------------------
# create_user / authenticate
# ---------------------------------------------------------------------------


def test_create_user_ok(perm_env) -> None:
    from custom_app.services.auth import create_user
    from custom_app.repositories import UserRepository
    uid = create_user(username="alice", password="pw1", display_name="Alice")
    assert uid.startswith("user_")
    user = UserRepository().find_by_id(uid)
    assert user is not None
    assert user["username"] == "alice"
    assert user["status"] == "active"


def test_create_user_duplicate_username_raises(perm_env) -> None:
    from custom_app.services.auth import create_user
    create_user(username="bob", password="pw")
    with pytest.raises(ValueError):
        create_user(username="bob", password="other")


def test_authenticate_success(perm_env) -> None:
    from custom_app.services.auth import authenticate, create_user
    create_user(username="carol", password="topsecret")
    user = authenticate("carol", "topsecret")
    assert user is not None
    assert user["username"] == "carol"


def test_authenticate_wrong_password(perm_env) -> None:
    from custom_app.services.auth import authenticate, create_user
    create_user(username="dave", password="right")
    assert authenticate("dave", "wrong") is None


def test_authenticate_unknown_user(perm_env) -> None:
    from custom_app.services.auth import authenticate
    assert authenticate("ghost", "x") is None


def test_authenticate_disabled_user_blocked(perm_env) -> None:
    from custom_app.db import now_iso
    from custom_app.repositories import UserRepository
    from custom_app.services.auth import authenticate, create_user
    uid = create_user(username="erin", password="pw")
    UserRepository().update_status(uid, status="disabled", updated_at=now_iso())
    assert authenticate("erin", "pw") is None


def test_authenticate_updates_last_login(perm_env) -> None:
    from custom_app.repositories import UserRepository
    from custom_app.services.auth import authenticate, create_user
    uid = create_user(username="frank", password="pw")
    before = UserRepository().find_by_id(uid)
    assert before["last_login_at"] == ""
    authenticate("frank", "pw")
    after = UserRepository().find_by_id(uid)
    assert after["last_login_at"] != ""


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def test_login_current_logout(app, perm_env) -> None:
    from custom_app.services.auth import (
        create_user, current_user, current_user_id, login_user, logout_user,
    )
    uid = create_user(username="gina", password="pw")
    with app.test_request_context():
        # 未登录
        assert current_user_id() is None
        assert current_user() is None
        # 登录
        login_user(uid)
        assert current_user_id() == uid
        u = current_user()
        assert u is not None
        assert u["user_id"] == uid
        # 登出
        logout_user()
        # g 缓存还是 truthy，需新 request context 才会重置
    with app.test_request_context():
        assert current_user_id() is None


def test_current_user_disabled_clears_session(app, perm_env) -> None:
    """登录后被禁用 → 下次 current_user() 返 None 并清 session。"""
    from custom_app.db import now_iso
    from custom_app.repositories import UserRepository
    from custom_app.services.auth import (
        create_user, current_user, current_user_id, login_user,
    )
    uid = create_user(username="hank", password="pw")
    with app.test_request_context():
        login_user(uid)
        UserRepository().update_status(uid, status="disabled", updated_at=now_iso())
        assert current_user() is None
        assert current_user_id() is None  # session 已清


# ---------------------------------------------------------------------------
# UserRepository: role 绑定 + KB 权限
# ---------------------------------------------------------------------------


def _seed_role_with_perm(role_id: str, kb_id: str, level: str = "read") -> None:
    """造一个角色 + 给它一个 KB 权限。"""
    from custom_app.db import now_iso
    from custom_app.repositories import RoleRepository
    repo = RoleRepository()
    if not repo.exists(role_id):
        repo.create(
            role_id=role_id, name=role_id, description="",
            created_at=now_iso(),
        )
    repo.upsert_permission(
        role_id=role_id, kb_id=kb_id, access_level=level,
        updated_at=now_iso(),
    )


def test_assign_revoke_role(perm_env) -> None:
    from custom_app.db import now_iso
    from custom_app.repositories import UserRepository
    from custom_app.services.auth import create_user
    uid = create_user(username="ivan", password="pw")
    _seed_role_with_perm("role_designer", "kb_a", "read")
    repo = UserRepository()
    repo.assign_role(user_id=uid, role_id="role_designer", created_at=now_iso())
    assert repo.list_role_ids_for_user(uid) == ["role_designer"]
    # 幂等
    repo.assign_role(user_id=uid, role_id="role_designer", created_at=now_iso())
    assert repo.list_role_ids_for_user(uid) == ["role_designer"]
    # revoke
    assert repo.revoke_role(user_id=uid, role_id="role_designer") is True
    assert repo.list_role_ids_for_user(uid) == []


def test_kb_permission_via_role(perm_env) -> None:
    from custom_app.db import now_iso
    from custom_app.repositories import UserRepository
    from custom_app.services.auth import create_user
    uid = create_user(username="jack", password="pw")
    _seed_role_with_perm("role_qc", "kb_quality", "read")
    UserRepository().assign_role(
        user_id=uid, role_id="role_qc", created_at=now_iso(),
    )
    repo = UserRepository()
    assert repo.user_has_kb_permission(
        user_id=uid, kb_id="kb_quality", min_level="read",
    ) is True
    # 未授权 KB
    assert repo.user_has_kb_permission(
        user_id=uid, kb_id="kb_other", min_level="read",
    ) is False
    # 要求 write 但只有 read
    assert repo.user_has_kb_permission(
        user_id=uid, kb_id="kb_quality", min_level="write",
    ) is False


def test_kb_permission_admin_satisfies_read(perm_env) -> None:
    """admin 权限自动满足 read / write 检查。"""
    from custom_app.db import now_iso
    from custom_app.repositories import UserRepository
    from custom_app.services.auth import create_user
    uid = create_user(username="ken", password="pw")
    _seed_role_with_perm("role_admin", "kb_admin", "admin")
    UserRepository().assign_role(
        user_id=uid, role_id="role_admin", created_at=now_iso(),
    )
    repo = UserRepository()
    assert repo.user_has_kb_permission(
        user_id=uid, kb_id="kb_admin", min_level="read",
    ) is True
    assert repo.user_has_kb_permission(
        user_id=uid, kb_id="kb_admin", min_level="write",
    ) is True
    assert repo.user_has_kb_permission(
        user_id=uid, kb_id="kb_admin", min_level="admin",
    ) is True


def test_list_kb_ids_accessible_dedup(perm_env) -> None:
    """同一 KB 被多个角色覆盖时去重。"""
    from custom_app.db import now_iso
    from custom_app.repositories import UserRepository
    from custom_app.services.auth import create_user
    uid = create_user(username="lara", password="pw")
    _seed_role_with_perm("role_a", "kb_shared", "read")
    _seed_role_with_perm("role_b", "kb_shared", "write")
    _seed_role_with_perm("role_b", "kb_only_b", "read")
    repo = UserRepository()
    repo.assign_role(user_id=uid, role_id="role_a", created_at=now_iso())
    repo.assign_role(user_id=uid, role_id="role_b", created_at=now_iso())
    kbs = set(repo.list_kb_ids_accessible(uid))
    assert kbs == {"kb_shared", "kb_only_b"}


# ---------------------------------------------------------------------------
# 装饰器：require_user
# ---------------------------------------------------------------------------


def test_require_user_unauthenticated_returns_401(app, perm_env) -> None:
    from custom_app.services.auth import require_user

    @app.route("/protected")
    @require_user
    def protected():
        return jsonify({"ok": True})

    client = app.test_client()
    r = client.get("/protected")
    assert r.status_code == 401
    assert r.get_json()["code"] == "AUTH_REQUIRED"


def test_require_user_authenticated_passes(app, perm_env) -> None:
    from custom_app.services.auth import create_user, login_user, require_user

    @app.route("/protected")
    @require_user
    def protected():
        return jsonify({"ok": True})

    uid = create_user(username="mary", password="pw")
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = uid
    r = client.get("/protected")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


# ---------------------------------------------------------------------------
# 装饰器：require_kb_permission
# ---------------------------------------------------------------------------


def test_require_kb_permission_unauthenticated_401(app, perm_env) -> None:
    from custom_app.services.auth import require_kb_permission

    @app.route("/kb/<kb_id>/ops")
    @require_kb_permission("write")
    def ops(kb_id):
        return jsonify({"kb": kb_id})

    r = app.test_client().get("/kb/anything/ops")
    assert r.status_code == 401


def test_require_kb_permission_no_perm_403(app, perm_env) -> None:
    from custom_app.services.auth import create_user, require_kb_permission

    @app.route("/kb/<kb_id>/ops")
    @require_kb_permission("read")
    def ops(kb_id):
        return jsonify({"kb": kb_id})

    uid = create_user(username="nick", password="pw")
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = uid
    r = client.get("/kb/secret/ops")
    assert r.status_code == 403
    assert r.get_json()["code"] == "KB_FORBIDDEN"


def test_require_kb_permission_granted_200(app, perm_env) -> None:
    from custom_app.db import now_iso
    from custom_app.repositories import UserRepository
    from custom_app.services.auth import create_user, require_kb_permission

    @app.route("/kb/<kb_id>/ops")
    @require_kb_permission("read")
    def ops(kb_id):
        return jsonify({"kb": kb_id})

    uid = create_user(username="opal", password="pw")
    _seed_role_with_perm("role_ops_test", "kb_ok", "read")
    UserRepository().assign_role(
        user_id=uid, role_id="role_ops_test", created_at=now_iso(),
    )
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = uid
    r = client.get("/kb/kb_ok/ops")
    assert r.status_code == 200
    assert r.get_json()["kb"] == "kb_ok"
