"""P-Perm Commit 5：管理后台用户 CRUD + 角色绑定 API。

路由前缀：/api/admin/users
鉴权：app.py 中 require_admin_token 拦截了 /api/admin/*（admin token）；
     登录用户名为 'admin' 的也算 admin，以便 UI 端不强制配置 ULTRARAG_ADMIN_TOKEN。

接口：
    GET    /api/admin/users                     列表
    POST   /api/admin/users                     创建（username, password, display_name）
    DELETE /api/admin/users/<user_id>           硬删用户 + 解绑所有角色
    POST   /api/admin/users/<user_id>/password  重置密码（{password}）
    POST   /api/admin/users/<user_id>/status    {status: active|disabled}
    GET    /api/admin/users/<user_id>/roles     已绑定的 role_id 列表
    POST   /api/admin/users/<user_id>/roles     绑定（{role_id}），幂等
    DELETE /api/admin/users/<user_id>/roles/<role_id>  解绑
"""

from __future__ import annotations

import functools
import hmac
import logging
import os
import uuid
from typing import Any, Callable

from flask import Blueprint, jsonify, request

from custom_app.db import new_id, now_iso
from custom_app.repositories import RoleRepository, UserRepository
from custom_app.services.auth import (
    current_user,
    create_user as create_user_service,
    hash_password,
)

logger = logging.getLogger(__name__)

admin_users_bp = Blueprint("admin_users_api", __name__)

ADMIN_USERNAME = "admin"


def _req_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"


def _ok(data: Any):
    return jsonify({"request_id": _req_id(), "data": data})


def _err(msg: str, code: str, status: int):
    return jsonify({"request_id": _req_id(), "error": msg, "code": code}), status


def _is_admin_token_present() -> bool:
    expected = os.getenv("ULTRARAG_ADMIN_TOKEN", "").strip()
    if not expected:
        return False
    presented = (request.headers.get("X-Admin-Token", "") or "").strip()
    return bool(presented) and hmac.compare_digest(presented, expected)


def _is_admin_user() -> bool:
    """登录用户名 admin 即视为管理员（与首启种子账号一致）。"""
    u = current_user()
    if not u:
        return False
    return (u.get("username") or "") == ADMIN_USERNAME


def require_admin(view: Callable) -> Callable:
    """Admin 守卫：admin token 或登录用户为 admin 才放行。"""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if _is_admin_token_present() or _is_admin_user():
            return view(*args, **kwargs)
        return _err("admin required", "ADMIN_REQUIRED", 403)
    return wrapped


def _public_user_view(row: dict[str, Any]) -> dict[str, Any]:
    """对外只暴露非敏感字段（剥掉 password_hash）。"""
    return {
        "user_id": row.get("user_id"),
        "username": row.get("username"),
        "display_name": row.get("display_name"),
        "status": row.get("status", "active"),
        "created_at": row.get("created_at"),
        "last_login_at": row.get("last_login_at", ""),
    }


# ── users CRUD ──────────────────────────────────────────────────────────────

@admin_users_bp.route("/api/admin/users", methods=["GET"])
@require_admin
def list_users():
    rows = UserRepository().list_active()
    return _ok({"items": [_public_user_view(r) for r in rows]})


@admin_users_bp.route("/api/admin/users", methods=["POST"])
@require_admin
def create_user():
    body = request.get_json(silent=True) or {}
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", "")).strip()
    display_name = str(body.get("display_name", "")).strip()
    if not username or not password:
        return _err("username + password required", "BAD_REQUEST", 400)
    if len(password) < 6:
        return _err("password too short (min 6)", "BAD_REQUEST", 400)
    try:
        uid = create_user_service(
            username=username, password=password, display_name=display_name,
        )
    except ValueError as e:
        return _err(str(e), "USERNAME_TAKEN", 409)
    row = UserRepository().find_by_id(uid)
    if not row:
        return _err("user vanished after create", "INTERNAL", 500)
    return _ok(_public_user_view(row))


@admin_users_bp.route("/api/admin/users/<user_id>", methods=["DELETE"])
@require_admin
def delete_user(user_id: str):
    uid = (user_id or "").strip()
    if not uid:
        return _err("user_id required", "BAD_REQUEST", 400)
    repo = UserRepository()
    row = repo.find_by_id(uid)
    if not row:
        return _err("user not found", "NOT_FOUND", 404)
    # 不允许删 admin（避免锁死后台）
    if (row.get("username") or "") == ADMIN_USERNAME:
        return _err("cannot delete admin user", "FORBIDDEN", 403)
    repo.delete(uid)
    logger.warning("admin deleted user: id=%s username=%s", uid, row.get("username"))
    return _ok({"user_id": uid})


@admin_users_bp.route("/api/admin/users/<user_id>/password", methods=["POST"])
@require_admin
def reset_password(user_id: str):
    body = request.get_json(silent=True) or {}
    new_pw = str(body.get("password", "")).strip()
    if len(new_pw) < 6:
        return _err("password too short (min 6)", "BAD_REQUEST", 400)
    repo = UserRepository()
    row = repo.find_by_id((user_id or "").strip())
    if not row:
        return _err("user not found", "NOT_FOUND", 404)
    repo.update_password(
        row["user_id"],
        password_hash=hash_password(new_pw),
        updated_at=now_iso(),
    )
    logger.warning("admin reset password: user=%s", row.get("username"))
    return _ok({"user_id": row["user_id"]})


@admin_users_bp.route("/api/admin/users/<user_id>/status", methods=["POST"])
@require_admin
def set_status(user_id: str):
    body = request.get_json(silent=True) or {}
    status = str(body.get("status", "")).strip()
    if status not in ("active", "disabled"):
        return _err("status must be active|disabled", "BAD_REQUEST", 400)
    repo = UserRepository()
    row = repo.find_by_id((user_id or "").strip())
    if not row:
        return _err("user not found", "NOT_FOUND", 404)
    if (row.get("username") or "") == ADMIN_USERNAME and status == "disabled":
        return _err("cannot disable admin user", "FORBIDDEN", 403)
    repo.update_status(row["user_id"], status=status, updated_at=now_iso())
    return _ok({"user_id": row["user_id"], "status": status})


# ── user ↔ role binding ─────────────────────────────────────────────────────

@admin_users_bp.route("/api/admin/users/<user_id>/roles", methods=["GET"])
@require_admin
def list_user_roles(user_id: str):
    uid = (user_id or "").strip()
    if not UserRepository().find_by_id(uid):
        return _err("user not found", "NOT_FOUND", 404)
    user_repo = UserRepository()
    role_repo = RoleRepository()
    role_ids = user_repo.list_role_ids_for_user(uid)
    items = []
    for rid in role_ids:
        r = role_repo.find_by_id(rid)
        if r:
            items.append({
                "role_id": r["role_id"],
                "name": r.get("name", ""),
                "description": r.get("description", ""),
            })
    return _ok({"items": items})


@admin_users_bp.route("/api/admin/users/<user_id>/roles", methods=["POST"])
@require_admin
def assign_role(user_id: str):
    body = request.get_json(silent=True) or {}
    role_id = str(body.get("role_id", "")).strip()
    uid = (user_id or "").strip()
    if not role_id:
        return _err("role_id required", "BAD_REQUEST", 400)
    if not UserRepository().find_by_id(uid):
        return _err("user not found", "NOT_FOUND", 404)
    if not RoleRepository().exists(role_id):
        return _err("role not found", "NOT_FOUND", 404)
    UserRepository().assign_role(
        user_id=uid, role_id=role_id, created_at=now_iso(),
    )
    return _ok({"user_id": uid, "role_id": role_id})


@admin_users_bp.route(
    "/api/admin/users/<user_id>/roles/<role_id>", methods=["DELETE"],
)
@require_admin
def revoke_role(user_id: str, role_id: str):
    uid = (user_id or "").strip()
    rid = (role_id or "").strip()
    if not UserRepository().find_by_id(uid):
        return _err("user not found", "NOT_FOUND", 404)
    UserRepository().revoke_role(user_id=uid, role_id=rid)
    return _ok({"user_id": uid, "role_id": rid})
