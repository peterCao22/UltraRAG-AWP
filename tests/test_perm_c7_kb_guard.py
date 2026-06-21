"""P-Perm C7：kb_id 守卫端到端越权测试。

涵盖：
    - chat_stream / chat / chat_markdown 必须有 read 才能调
    - POST /api/sessions 必须有 read 才能开会话
    - /api/kb/<kb_id> GET 必须有 read 才能看详情
    - 写操作（PUT/DELETE/upload/ingest 等）必须有 write
    - 用户名 admin 超管语义：所有都放行
    - admin token bypass：所有都放行
    - 普通用户对未授权 KB → 403 KB_FORBIDDEN
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")
    monkeypatch.setenv("ULTRARAG_ADMIN_TOKEN", "")
    monkeypatch.setenv("ULTRARAG_FLASK_SECRET_KEY", "c7-secret")
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
    monkeypatch.setenv("ULTRARAG_ADMIN_TOKEN", "c7-token")
    monkeypatch.setenv("ULTRARAG_FLASK_SECRET_KEY", "c7-secret")
    from custom_app.repositories.base import set_default_provider
    set_default_provider(None)
    from custom_app.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _seed_user(username: str, password: str = "pw123456") -> str:
    from custom_app.services.auth import create_user
    u = create_user(
        username=username, password=password, display_name=username,
    )
    return u["user_id"] if isinstance(u, dict) else str(u)


def _seed_kb(kb_id: str) -> None:
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


def _grant_kb(user_id: str, kb_id: str, level: str = "read") -> str:
    from custom_app.db import new_id, now_iso
    from custom_app.repositories import RoleRepository, UserRepository
    rid = new_id("role")
    ts = now_iso()
    RoleRepository().create(
        role_id=rid, name=f"role-{rid}", description="", created_at=ts,
    )
    RoleRepository().upsert_permission(
        role_id=rid, kb_id=kb_id, access_level=level, updated_at=ts,
    )
    UserRepository().assign_role(user_id=user_id, role_id=rid, created_at=ts)
    return rid


def _login(client, username: str, password: str = "pw123456") -> None:
    r = client.post(
        "/api/auth/login", json={"username": username, "password": password},
    )
    assert r.status_code == 200, r.get_data(as_text=True)


# ── chat_stream / chat / chat_markdown ─────────────────────────────────────

def test_chat_stream_forbids_without_kb_permission(client) -> None:
    """alice 没 kb_secret 权限 → POST /api/chat/stream 403 KB_FORBIDDEN。"""
    _seed_user("alice")
    _seed_kb("kb_secret")
    _login(client, "alice")
    r = client.post(
        "/api/chat/stream",
        json={"kb_id": "kb_secret", "question": "leak me"},
    )
    assert r.status_code == 403
    body = r.get_json() or {}
    assert body.get("code") == "KB_FORBIDDEN"


def test_chat_forbids_without_kb_permission(client) -> None:
    _seed_user("alice")
    _seed_kb("kb_secret")
    _login(client, "alice")
    r = client.post(
        "/api/chat", json={"kb_id": "kb_secret", "question": "x"},
    )
    assert r.status_code == 403
    assert r.get_json().get("code") == "KB_FORBIDDEN"


def test_chat_markdown_forbids_without_kb_permission(client) -> None:
    _seed_user("alice")
    _seed_kb("kb_secret")
    _login(client, "alice")
    r = client.post(
        "/api/chat/markdown",
        json={"kb_id": "kb_secret", "question": "x"},
    )
    assert r.status_code == 403
    assert r.get_json().get("code") == "KB_FORBIDDEN"


def test_admin_username_bypasses_chat_guard(client) -> None:
    """admin 用户超管语义 → chat 不被 KB 守卫拦（其他错误如 KB 不存在另说）。"""
    _login(client, "admin", "admin123")
    r = client.post(
        "/api/chat/stream",
        json={"kb_id": "kb_anything", "question": "x"},
    )
    # 不应该被守卫拦：守卫拦的 status 是 403 KB_FORBIDDEN。
    # 实际可能是 200 SSE / 4xx 别的错。只确认不是 KB_FORBIDDEN。
    if r.status_code == 403:
        body = r.get_json() or {}
        assert body.get("code") != "KB_FORBIDDEN", body


def test_admin_token_bypasses_chat_guard(admin_token_client) -> None:
    """X-Admin-Token → chat 不被守卫拦。"""
    r = admin_token_client.post(
        "/api/chat/stream",
        json={"kb_id": "kb_anything", "question": "x"},
        headers={"X-Admin-Token": "c7-token"},
    )
    if r.status_code == 403:
        body = r.get_json() or {}
        assert body.get("code") != "KB_FORBIDDEN", body


def test_chat_stream_allowed_after_grant(client) -> None:
    """授予 read 后不再被守卫拦（可能因运行时其他错误 4xx/5xx，仅校验不是 403 KB_FORBIDDEN）。"""
    uid = _seed_user("alice")
    _seed_kb("kb_allowed")
    _grant_kb(uid, "kb_allowed", "read")
    _login(client, "alice")
    r = client.post(
        "/api/chat/stream",
        json={"kb_id": "kb_allowed", "question": "x"},
    )
    if r.status_code == 403:
        body = r.get_json() or {}
        assert body.get("code") != "KB_FORBIDDEN", body


# ── sessions POST ──────────────────────────────────────────────────────────

def test_sessions_post_forbids_without_kb_permission(client) -> None:
    _seed_user("alice")
    _seed_kb("kb_secret")
    _login(client, "alice")
    r = client.post(
        "/api/sessions",
        json={"kb_id": "kb_secret", "title": "spy"},
    )
    assert r.status_code == 403
    body = r.get_json() or {}
    assert body.get("code") == "KB_FORBIDDEN"


# ── /api/kb/<kb_id> 读路径 ────────────────────────────────────────────────

def test_kb_get_single_forbids_without_permission(client) -> None:
    _seed_user("alice")
    _seed_kb("kb_secret")
    _login(client, "alice")
    r = client.get("/api/kb/kb_secret")
    assert r.status_code == 403
    assert r.get_json().get("code") == "KB_FORBIDDEN"


def test_kb_documents_list_forbids_without_permission(client) -> None:
    _seed_user("alice")
    _seed_kb("kb_secret")
    _login(client, "alice")
    r = client.get("/api/kb/kb_secret/documents")
    assert r.status_code == 403


def test_kb_chunks_forbids_without_permission(client) -> None:
    _seed_user("alice")
    _seed_kb("kb_secret")
    _login(client, "alice")
    r = client.get("/api/kb/kb_secret/chunks")
    assert r.status_code == 403


def test_kb_agent_config_forbids_without_permission(client) -> None:
    _seed_user("alice")
    _seed_kb("kb_secret")
    _login(client, "alice")
    r = client.get("/api/kb/kb_secret/agent_config")
    assert r.status_code == 403


# ── /api/kb/<kb_id> 写路径 ────────────────────────────────────────────────

def test_kb_delete_forbids_with_only_read(client) -> None:
    """alice 只有 read → DELETE /api/kb/<kb_id> 403（需要 write）。"""
    uid = _seed_user("alice")
    _seed_kb("kb_partial")
    _grant_kb(uid, "kb_partial", "read")
    _login(client, "alice")
    r = client.delete("/api/kb/kb_partial")
    assert r.status_code == 403
    assert r.get_json().get("code") == "KB_FORBIDDEN"


def test_kb_update_forbids_with_only_read(client) -> None:
    uid = _seed_user("alice")
    _seed_kb("kb_partial")
    _grant_kb(uid, "kb_partial", "read")
    _login(client, "alice")
    r = client.put("/api/kb/kb_partial", json={"name": "rename"})
    assert r.status_code == 403


def test_kb_write_allowed_with_write_grant(client) -> None:
    """alice 有 write → PUT 不再被守卫拦（可能因 KB 不存在 404，不应是 KB_FORBIDDEN）。"""
    uid = _seed_user("alice")
    _seed_kb("kb_full")
    _grant_kb(uid, "kb_full", "write")
    _login(client, "alice")
    r = client.put("/api/kb/kb_full", json={"name": "rename"})
    if r.status_code == 403:
        body = r.get_json() or {}
        assert body.get("code") != "KB_FORBIDDEN", body


# ── admin / admin token bypass ────────────────────────────────────────────

def test_admin_user_can_read_any_kb(client) -> None:
    _seed_kb("kb_isolated")
    _login(client, "admin", "admin123")
    r = client.get("/api/kb/kb_isolated")
    assert r.status_code == 200


def test_admin_user_can_delete_any_kb(client) -> None:
    _seed_kb("kb_doomed")
    _login(client, "admin", "admin123")
    r = client.delete("/api/kb/kb_doomed")
    # delete 视图层可能因 KB 在删除中或文件清理失败返非 200，但不应是 403
    assert r.status_code != 403


def test_admin_token_can_read_any_kb(admin_token_client) -> None:
    _seed_kb("kb_isolated")
    r = admin_token_client.get(
        "/api/kb/kb_isolated", headers={"X-Admin-Token": "c7-token"},
    )
    assert r.status_code == 200
