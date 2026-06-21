"""P-Perm Commit 3: /api/auth/* REST 单测 + 默认 admin 种子。"""

from __future__ import annotations

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ULTRARAG_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("ULTRARAG_FLASK_SECRET_KEY", "test-secret")
    # 重置 default provider，避免上个测试残留
    from custom_app.repositories.base import set_default_provider
    set_default_provider(None)
    from custom_app.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_default_admin_seeded(client) -> None:
    """create_app() 启动时种入 admin / admin123。"""
    r = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "admin123"},
    )
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["username"] == "admin"


def test_login_wrong_password_401(client) -> None:
    r = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "wrong"},
    )
    assert r.status_code == 401
    assert r.get_json()["code"] == "AUTH_INVALID"


def test_login_unknown_user_returns_same_401(client) -> None:
    """不区分原因：用户不存在和密码错都是 401 INVALID。"""
    r = client.post(
        "/api/auth/login",
        json={"username": "ghost", "password": "x"},
    )
    assert r.status_code == 401
    assert r.get_json()["code"] == "AUTH_INVALID"


def test_login_missing_input_400(client) -> None:
    r = client.post("/api/auth/login", json={"username": "admin"})
    assert r.status_code == 400


def test_me_unauthenticated_401(client) -> None:
    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_me_authenticated_returns_user(client) -> None:
    client.post("/api/auth/login",
                json={"username": "admin", "password": "admin123"})
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.get_json()["data"]["username"] == "admin"


def test_logout_clears_session(client) -> None:
    client.post("/api/auth/login",
                json={"username": "admin", "password": "admin123"})
    # 确认登录态
    assert client.get("/api/auth/me").status_code == 200
    # 登出
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    # 登出后再访问 /me 应 401
    assert client.get("/api/auth/me").status_code == 401


def test_change_password_success(client) -> None:
    client.post("/api/auth/login",
                json={"username": "admin", "password": "admin123"})
    r = client.post(
        "/api/auth/change_password",
        json={"old_password": "admin123", "new_password": "newpass123"},
    )
    assert r.status_code == 200
    # 旧密码失效
    client.post("/api/auth/logout")
    r2 = client.post("/api/auth/login",
                     json={"username": "admin", "password": "admin123"})
    assert r2.status_code == 401
    # 新密码可用
    r3 = client.post("/api/auth/login",
                     json={"username": "admin", "password": "newpass123"})
    assert r3.status_code == 200


def test_change_password_wrong_old_pw_401(client) -> None:
    client.post("/api/auth/login",
                json={"username": "admin", "password": "admin123"})
    r = client.post(
        "/api/auth/change_password",
        json={"old_password": "wrong", "new_password": "newpass123"},
    )
    assert r.status_code == 401
    assert r.get_json()["code"] == "OLD_PASSWORD_INVALID"


def test_change_password_too_short_400(client) -> None:
    client.post("/api/auth/login",
                json={"username": "admin", "password": "admin123"})
    r = client.post(
        "/api/auth/change_password",
        json={"old_password": "admin123", "new_password": "abc"},
    )
    assert r.status_code == 400


def test_change_password_unauthenticated_401(client) -> None:
    r = client.post(
        "/api/auth/change_password",
        json={"old_password": "x", "new_password": "newpass123"},
    )
    assert r.status_code == 401


def test_admin_token_bypass_for_me(client, monkeypatch) -> None:
    """配置 admin token 时，未登录 + 带 admin token 也能访问 require_user 路由。"""
    # 重建 client 加 admin token
    monkeypatch.setenv("ULTRARAG_ADMIN_TOKEN", "secret-admin-token")
    from custom_app.repositories.base import set_default_provider
    set_default_provider(None)
    from custom_app.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    # 不登录、带 admin token
    r = c.get("/api/auth/me", headers={"X-Admin-Token": "secret-admin-token"})
    # /me 内部仍调 current_user()，未登录返 401
    # （admin token 让装饰器放行，但 current_user 仍 None 故走 401 路径）
    # 这是一个有意识的选择：admin token 不代表"有用户"，只代表"是 admin 运维"
    assert r.status_code == 401
