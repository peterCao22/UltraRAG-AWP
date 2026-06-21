"""P-Perm Auth REST API：登录 / 登出 / 当前用户 / 修改密码。

路由（不需要 admin token，但需要 Flask SECRET_KEY 才能签 session cookie）：
    POST /api/auth/login          { username, password } → 200 + user dict / 401
    POST /api/auth/logout         → 200
    GET  /api/auth/me             已登录 → user dict / 401
    POST /api/auth/change_password { old_password, new_password } → 200 / 401
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from custom_app.db import now_iso
from custom_app.repositories import UserRepository
from custom_app.services.auth import (
    authenticate,
    current_user,
    hash_password,
    login_user,
    logout_user,
    require_user,
    verify_password,
)

logger = logging.getLogger(__name__)
auth_bp = Blueprint("auth_api", __name__)


def _err(msg: str, code: str, status: int):
    return jsonify({"error": msg, "code": code}), status


def _user_public(user: dict) -> dict:
    """返给前端的用户字段（去掉 password_hash）。"""
    return {
        "user_id": user.get("user_id"),
        "username": user.get("username"),
        "display_name": user.get("display_name") or user.get("username"),
        "status": user.get("status"),
        "last_login_at": user.get("last_login_at"),
    }


@auth_bp.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if not username or not password:
        return _err("username and password required", "AUTH_INPUT_REQUIRED", 400)
    user = authenticate(username, password)
    if user is None:
        # 401 不区分原因（避免枚举攻击）
        return _err("invalid credentials", "AUTH_INVALID", 401)
    login_user(str(user["user_id"]))
    logger.info("login ok user=%s", user["user_id"])
    return jsonify({"data": _user_public(user)})


@auth_bp.route("/api/auth/logout", methods=["POST"])
def logout():
    logout_user()
    return jsonify({"data": {"ok": True}})


@auth_bp.route("/api/auth/me", methods=["GET"])
@require_user
def me():
    user = current_user()
    if user is None:
        # require_user 已经拦住 admin token 路径；这里 user 可能为 None（极少）
        return _err("login required", "AUTH_REQUIRED", 401)
    return jsonify({"data": _user_public(user)})


@auth_bp.route("/api/auth/change_password", methods=["POST"])
@require_user
def change_password():
    user = current_user()
    if user is None:
        return _err("login required", "AUTH_REQUIRED", 401)
    data = request.get_json(silent=True) or {}
    old_pw = str(data.get("old_password") or "")
    new_pw = str(data.get("new_password") or "")
    if not old_pw or not new_pw:
        return _err("old_password and new_password required",
                    "PASSWORD_INPUT_REQUIRED", 400)
    if len(new_pw) < 6:
        return _err("new password too short (min 6)", "PASSWORD_TOO_SHORT", 400)
    if not verify_password(old_pw, str(user.get("password_hash") or "")):
        return _err("old password incorrect", "OLD_PASSWORD_INVALID", 401)
    UserRepository().update_password(
        str(user["user_id"]),
        password_hash=hash_password(new_pw),
        updated_at=now_iso(),
    )
    logger.info("password changed user=%s", user["user_id"])
    return jsonify({"data": {"ok": True}})
