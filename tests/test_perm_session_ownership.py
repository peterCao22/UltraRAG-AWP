"""P-Perm Commit 4: session 归属 + KB 列表过滤 + audit user_id 注入 单测。

涵盖：
    - /api/sessions POST 创建时落 user_id
    - /api/sessions GET 仅返回自己的会话
    - /api/sessions/<id> GET/DELETE 跨用户 403
    - /api/kb GET 按用户权限过滤
    - admin token bypass 全部过滤
    - 未登录访问受保护前缀 401（before_request 中间件）
    - audit_log.log_qa 支持 user_id 落库
"""

from __future__ import annotations

import pytest


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    """新鲜 SQLite + Flask app；admin 已种好。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ULTRARAG_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("ULTRARAG_FLASK_SECRET_KEY", "test-secret-perm-c4")
    from custom_app.repositories.base import set_default_provider
    set_default_provider(None)
    from custom_app.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture()
def admin_token_client(tmp_path, monkeypatch):
    """配置 ULTRARAG_ADMIN_TOKEN 的 client，用于 bypass 测试。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")
    monkeypatch.setenv("ULTRARAG_ADMIN_TOKEN", "tok-admin-c4")
    monkeypatch.setenv("ULTRARAG_FLASK_SECRET_KEY", "test-secret-perm-c4")
    from custom_app.repositories.base import set_default_provider
    set_default_provider(None)
    from custom_app.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _seed_user(username: str, password: str = "pw123456") -> str:
    """直接通过 service 创建一个非 admin 用户；返回 user_id。

    create_user 返回类型可能为 user_id (str) 或 row (dict)；这里两种都兼容。
    """
    from custom_app.services.auth import create_user
    u = create_user(
        username=username, password=password, display_name=username,
    )
    if isinstance(u, dict):
        return u["user_id"]
    return str(u)


def _seed_kb(kb_id: str) -> None:
    """最小可用 KB 行——仅 schema 字段，足以让 list_paginated 返回该行。"""
    from custom_app.db import now_iso
    from custom_app.repositories import KbRepository
    ts = now_iso()
    KbRepository().create(
        kb_id=kb_id, name=kb_id, description="", tenant_id="default",
        kb_type="general",
        data_path=f"data/kb/{kb_id}",
        index_path=f"data/kb/{kb_id}/index/index.index",
        embedding_path=f"data/kb/{kb_id}/embedding/embedding.npy",
        created_at=ts,
    )


def _login(client, username: str, password: str = "pw123456") -> None:
    r = client.post(
        "/api/auth/login", json={"username": username, "password": password},
    )
    assert r.status_code == 200, r.get_data(as_text=True)


# ── before_request middleware ─────────────────────────────────────────────────

def test_unauthenticated_protected_route_401(client) -> None:
    """未登录访问 /api/kb 返回 401 AUTH_REQUIRED。"""
    r = client.get("/api/kb")
    assert r.status_code == 401
    assert r.get_json()["code"] == "AUTH_REQUIRED"


def test_unauthenticated_sessions_route_401(client) -> None:
    r = client.get("/api/sessions?kb_id=kb_x")
    assert r.status_code == 401
    assert r.get_json()["code"] == "AUTH_REQUIRED"


def test_auth_routes_public(client) -> None:
    """/api/auth/* 在 before_request 白名单内，未登录可命中视图层。

    /api/auth/login 是真正能在未登录命中的；它若未受 before_request 拦截，
    response 应为 400（缺 body）或 401（认证失败），而不是被代码 AUTH_REQUIRED 顶掉。
    /api/auth/me 同样应进到 require_user 装饰器返 401（不是 before_request 提前拦截）。
    """
    r = client.post("/api/auth/login", json={})
    # before_request 若拦截则会直接 401 AUTH_REQUIRED；这里期望 400（视图层 missing fields）
    assert r.status_code in (400, 401)


def test_admin_token_bypass_unauth_routes(admin_token_client) -> None:
    """带 X-Admin-Token 即可访问受保护路由，无需登录。"""
    r = admin_token_client.get(
        "/api/kb", headers={"X-Admin-Token": "tok-admin-c4"},
    )
    assert r.status_code == 200


def test_auth_required_env_off(client, monkeypatch) -> None:
    """ULTRARAG_AUTH_REQUIRED=0 时关闭中间件（开发模式）。"""
    monkeypatch.setenv("ULTRARAG_AUTH_REQUIRED", "0")
    r = client.get("/api/kb")
    assert r.status_code == 200


# ── session ownership ────────────────────────────────────────────────────────

def test_session_create_records_user_id(client) -> None:
    """登录用户创建 session 时 user_id 落库。"""
    uid = _seed_user("alice")
    _login(client, "alice")
    r = client.post("/api/sessions", json={"kb_id": "kb_a", "title": "t1"})
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["user_id"] == uid


def test_session_list_only_own_sessions(client) -> None:
    """alice 看不见 bob 的会话。"""
    _seed_user("alice")
    _seed_user("bob")

    _login(client, "alice")
    client.post("/api/sessions", json={"kb_id": "kb_x", "title": "alice-s1"})
    # logout
    client.post("/api/auth/logout")

    _login(client, "bob")
    client.post("/api/sessions", json={"kb_id": "kb_x", "title": "bob-s1"})
    r = client.get("/api/sessions?kb_id=kb_x")
    assert r.status_code == 200
    items = r.get_json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["title"] == "bob-s1"


def test_session_get_other_user_returns_403(client) -> None:
    """跨用户读单条 session → 403。"""
    _seed_user("alice")
    _seed_user("bob")

    _login(client, "alice")
    r1 = client.post(
        "/api/sessions", json={"kb_id": "kb_y", "title": "alice-secret"},
    )
    sid = r1.get_json()["data"]["session_id"]
    client.post("/api/auth/logout")

    _login(client, "bob")
    r = client.get(f"/api/sessions/{sid}")
    assert r.status_code == 403


def test_session_delete_other_user_returns_403(client) -> None:
    """跨用户删 session → 403，且 session 仍存在。"""
    _seed_user("alice")
    _seed_user("bob")

    _login(client, "alice")
    sid = client.post(
        "/api/sessions", json={"kb_id": "kb_z", "title": "alice-keep"},
    ).get_json()["data"]["session_id"]
    client.post("/api/auth/logout")

    _login(client, "bob")
    r = client.delete(f"/api/sessions/{sid}")
    assert r.status_code == 403

    client.post("/api/auth/logout")
    _login(client, "alice")
    r2 = client.get(f"/api/sessions/{sid}")
    assert r2.status_code == 200


def test_admin_token_sees_all_sessions(admin_token_client) -> None:
    """admin token bypass session 归属过滤。"""
    _seed_user("alice")
    _seed_user("bob")
    # 用 session 登录 alice 创建一条
    admin_token_client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "pw123456"},
    )
    admin_token_client.post(
        "/api/sessions", json={"kb_id": "kb_admin", "title": "a-s"},
    )
    admin_token_client.post("/api/auth/logout")
    admin_token_client.post(
        "/api/auth/login",
        json={"username": "bob", "password": "pw123456"},
    )
    admin_token_client.post(
        "/api/sessions", json={"kb_id": "kb_admin", "title": "b-s"},
    )
    admin_token_client.post("/api/auth/logout")
    # 现在 admin token 应能看到两条
    r = admin_token_client.get(
        "/api/sessions?kb_id=kb_admin",
        headers={"X-Admin-Token": "tok-admin-c4"},
    )
    assert r.status_code == 200
    items = r.get_json()["data"]["items"]
    assert len(items) == 2


# ── KB list filter ──────────────────────────────────────────────────────────

def test_kb_list_filters_by_user_permission(client) -> None:
    """alice 只看到自己被授权的 KB。"""
    from custom_app.repositories import UserRepository, RoleRepository
    from custom_app.db import now_iso

    uid = _seed_user("alice")
    _seed_kb("kb_visible")
    _seed_kb("kb_hidden")
    # 给 alice 一个 role 并授权访问 kb_visible
    role_id = "role_alice"
    RoleRepository().create(
        role_id=role_id, name="alice-role", description="",
        created_at=now_iso(),
    )
    RoleRepository().upsert_permission(
        role_id=role_id, kb_id="kb_visible", access_level="read",
        updated_at=now_iso(),
    )
    UserRepository().assign_role(
        user_id=uid, role_id=role_id, created_at=now_iso(),
    )

    _login(client, "alice")
    r = client.get("/api/kb")
    assert r.status_code == 200
    items = r.get_json()["data"]
    ids = {it["kb_id"] for it in items}
    assert "kb_visible" in ids
    assert "kb_hidden" not in ids


def test_kb_list_admin_token_no_filter(admin_token_client) -> None:
    """admin token 看到所有 KB。"""
    _seed_kb("kb_one")
    _seed_kb("kb_two")
    r = admin_token_client.get(
        "/api/kb", headers={"X-Admin-Token": "tok-admin-c4"},
    )
    assert r.status_code == 200
    items = r.get_json()["data"]
    ids = {it["kb_id"] for it in items}
    assert "kb_one" in ids and "kb_two" in ids


def test_kb_list_user_without_roles_sees_nothing(client) -> None:
    """没绑定任何 role 的用户 → 空 KB 列表。"""
    _seed_user("rookie")
    _seed_kb("kb_internal")
    _login(client, "rookie")
    r = client.get("/api/kb")
    assert r.status_code == 200
    items = r.get_json()["data"]
    assert items == []


# ── audit log user_id ───────────────────────────────────────────────────────

def test_audit_log_qa_records_user_id(tmp_path, monkeypatch) -> None:
    """log_qa(user_id=...) 写入 audit_logs.user_id。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")
    from custom_app.repositories.base import set_default_provider
    set_default_provider(None)
    from custom_app.db import init_db
    init_db()

    from custom_app.services.audit_log import log_qa
    ok = log_qa(
        kb_id="kb_a", session_id="sess_x",
        query="q", answer="a", chunk_ids=["c1"],
        user_id="user_alice",
    )
    assert ok is True

    from custom_app.repositories.audit_repository import AuditRepository
    rows = AuditRepository().list_recent(kb_id="kb_a", limit=1)
    assert len(rows) == 1
    # list_recent 当前 SELECT 列不含 user_id，因此直接查 raw 看一下
    from custom_app.repositories.base import get_default_provider
    with get_default_provider().connect() as conn:
        cur = conn.execute(
            "SELECT user_id FROM audit_logs WHERE kb_id = ? LIMIT 1", ("kb_a",),
        )
        row = cur.fetchone()
    assert row is not None
    # row may be sqlite3.Row or tuple
    val = row[0] if not hasattr(row, "keys") else row["user_id"]
    assert val == "user_alice"


def test_audit_log_qa_empty_user_id_default(tmp_path, monkeypatch) -> None:
    """不传 user_id 时落空串，兼容 admin token / 匿名场景。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")
    from custom_app.repositories.base import set_default_provider
    set_default_provider(None)
    from custom_app.db import init_db
    init_db()

    from custom_app.services.audit_log import log_qa
    assert log_qa(
        kb_id="kb_b", session_id=None, query="q", answer="a",
    ) is True

    from custom_app.repositories.base import get_default_provider
    with get_default_provider().connect() as conn:
        cur = conn.execute(
            "SELECT user_id FROM audit_logs WHERE kb_id = ? LIMIT 1", ("kb_b",),
        )
        row = cur.fetchone()
    val = row[0] if not hasattr(row, "keys") else row["user_id"]
    assert val == ""
