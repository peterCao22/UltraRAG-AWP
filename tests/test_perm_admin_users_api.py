"""P-Perm Commit 5：/api/admin/users/* CRUD + 角色绑定 单测。

涵盖：
    - 列表 / 创建 / 删除 / 重置密码 / 改状态
    - 绑定 role / 解绑 role / 列出 role
    - admin 守卫：未登录 + 无 admin token → 403
    - 不能删 / 禁用 admin 用户（防锁死）
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ULTRARAG_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("ULTRARAG_FLASK_SECRET_KEY", "test-secret-c5")
    from custom_app.repositories.base import set_default_provider
    set_default_provider(None)
    from custom_app.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture()
def admin_token_client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")
    monkeypatch.setenv("ULTRARAG_ADMIN_TOKEN", "tok-c5")
    monkeypatch.setenv("ULTRARAG_FLASK_SECRET_KEY", "test-secret-c5")
    from custom_app.repositories.base import set_default_provider
    set_default_provider(None)
    from custom_app.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _login_admin(client) -> None:
    r = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "admin123"},
    )
    assert r.status_code == 200, r.get_data(as_text=True)


# ── 鉴权 ────────────────────────────────────────────────────────────────────

def test_list_users_requires_admin(client) -> None:
    """未登录 + 无 admin token → 403。"""
    r = client.get("/api/admin/users")
    assert r.status_code == 403
    assert r.get_json()["code"] == "ADMIN_REQUIRED"


def test_non_admin_user_denied(client) -> None:
    """普通用户登录访问 → 403。"""
    from custom_app.services.auth import create_user
    create_user(username="alice", password="pw123456", display_name="A")
    client.post(
        "/api/auth/login", json={"username": "alice", "password": "pw123456"},
    )
    r = client.get("/api/admin/users")
    assert r.status_code == 403


def test_admin_token_can_access(admin_token_client) -> None:
    """X-Admin-Token 直接进入。"""
    r = admin_token_client.get(
        "/api/admin/users", headers={"X-Admin-Token": "tok-c5"},
    )
    assert r.status_code == 200


# ── CRUD ────────────────────────────────────────────────────────────────────

def test_list_users_returns_admin_seed(client) -> None:
    _login_admin(client)
    r = client.get("/api/admin/users")
    assert r.status_code == 200
    items = r.get_json()["data"]["items"]
    usernames = {it["username"] for it in items}
    assert "admin" in usernames


def test_create_user_happy(client) -> None:
    _login_admin(client)
    r = client.post(
        "/api/admin/users",
        json={"username": "newbie", "password": "secret1", "display_name": "N"},
    )
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["username"] == "newbie"
    assert data["status"] == "active"
    # 安全：返回里不应有 password_hash
    assert "password_hash" not in data


def test_create_user_short_password_rejected(client) -> None:
    _login_admin(client)
    r = client.post(
        "/api/admin/users",
        json={"username": "shortpw", "password": "abc"},
    )
    assert r.status_code == 400


def test_create_user_duplicate_username_409(client) -> None:
    _login_admin(client)
    client.post(
        "/api/admin/users",
        json={"username": "dup", "password": "pw123456"},
    )
    r = client.post(
        "/api/admin/users",
        json={"username": "dup", "password": "pw123456"},
    )
    assert r.status_code == 409
    assert r.get_json()["code"] == "USERNAME_TAKEN"


def test_delete_user_happy(client) -> None:
    _login_admin(client)
    r1 = client.post(
        "/api/admin/users",
        json={"username": "willdel", "password": "pw123456"},
    )
    uid = r1.get_json()["data"]["user_id"]
    r2 = client.delete(f"/api/admin/users/{uid}")
    assert r2.status_code == 200
    r3 = client.get("/api/admin/users")
    usernames = {it["username"] for it in r3.get_json()["data"]["items"]}
    assert "willdel" not in usernames


def test_cannot_delete_admin(client) -> None:
    _login_admin(client)
    from custom_app.repositories import UserRepository
    admin_row = UserRepository().find_by_username("admin")
    r = client.delete(f"/api/admin/users/{admin_row['user_id']}")
    assert r.status_code == 403


def test_reset_password_works(client) -> None:
    _login_admin(client)
    r1 = client.post(
        "/api/admin/users",
        json={"username": "pwreset", "password": "old1234"},
    )
    uid = r1.get_json()["data"]["user_id"]
    r2 = client.post(
        f"/api/admin/users/{uid}/password", json={"password": "new1234"},
    )
    assert r2.status_code == 200
    # 旧密码登录失败
    client.post("/api/auth/logout")
    r_old = client.post(
        "/api/auth/login",
        json={"username": "pwreset", "password": "old1234"},
    )
    assert r_old.status_code == 401
    # 新密码登录成功
    r_new = client.post(
        "/api/auth/login",
        json={"username": "pwreset", "password": "new1234"},
    )
    assert r_new.status_code == 200


def test_set_status_disable_blocks_login(client) -> None:
    _login_admin(client)
    r1 = client.post(
        "/api/admin/users",
        json={"username": "dis", "password": "pw123456"},
    )
    uid = r1.get_json()["data"]["user_id"]
    client.post(
        f"/api/admin/users/{uid}/status", json={"status": "disabled"},
    )
    client.post("/api/auth/logout")
    r = client.post(
        "/api/auth/login",
        json={"username": "dis", "password": "pw123456"},
    )
    assert r.status_code == 401


def test_cannot_disable_admin(client) -> None:
    _login_admin(client)
    from custom_app.repositories import UserRepository
    admin_row = UserRepository().find_by_username("admin")
    r = client.post(
        f"/api/admin/users/{admin_row['user_id']}/status",
        json={"status": "disabled"},
    )
    assert r.status_code == 403


# ── 角色绑定 ────────────────────────────────────────────────────────────────

def _seed_role(role_id: str, name: str) -> None:
    from custom_app.repositories import RoleRepository
    from custom_app.db import now_iso
    RoleRepository().create(
        role_id=role_id, name=name, description="", created_at=now_iso(),
    )


def test_assign_and_list_roles(client) -> None:
    _login_admin(client)
    r1 = client.post(
        "/api/admin/users",
        json={"username": "rbob", "password": "pw123456"},
    )
    uid = r1.get_json()["data"]["user_id"]
    _seed_role("role_x", "X")
    _seed_role("role_y", "Y")
    client.post(
        f"/api/admin/users/{uid}/roles", json={"role_id": "role_x"},
    )
    client.post(
        f"/api/admin/users/{uid}/roles", json={"role_id": "role_y"},
    )
    r = client.get(f"/api/admin/users/{uid}/roles")
    items = r.get_json()["data"]["items"]
    role_ids = {it["role_id"] for it in items}
    assert role_ids == {"role_x", "role_y"}


def test_assign_role_idempotent(client) -> None:
    _login_admin(client)
    uid = client.post(
        "/api/admin/users",
        json={"username": "rid", "password": "pw123456"},
    ).get_json()["data"]["user_id"]
    _seed_role("role_dup", "Dup")
    client.post(f"/api/admin/users/{uid}/roles", json={"role_id": "role_dup"})
    r = client.post(
        f"/api/admin/users/{uid}/roles", json={"role_id": "role_dup"},
    )
    assert r.status_code == 200
    items = client.get(
        f"/api/admin/users/{uid}/roles",
    ).get_json()["data"]["items"]
    assert len(items) == 1


def test_revoke_role(client) -> None:
    _login_admin(client)
    uid = client.post(
        "/api/admin/users",
        json={"username": "rrev", "password": "pw123456"},
    ).get_json()["data"]["user_id"]
    _seed_role("role_to_rev", "R")
    client.post(
        f"/api/admin/users/{uid}/roles", json={"role_id": "role_to_rev"},
    )
    r = client.delete(f"/api/admin/users/{uid}/roles/role_to_rev")
    assert r.status_code == 200
    items = client.get(
        f"/api/admin/users/{uid}/roles",
    ).get_json()["data"]["items"]
    assert items == []


def test_assign_unknown_role_404(client) -> None:
    _login_admin(client)
    uid = client.post(
        "/api/admin/users",
        json={"username": "ghostr", "password": "pw123456"},
    ).get_json()["data"]["user_id"]
    r = client.post(
        f"/api/admin/users/{uid}/roles", json={"role_id": "role_ghost"},
    )
    assert r.status_code == 404
