"""P-Perm 认证 service：bcrypt 密码 + Flask Session + 装饰器。

设计：
    - 密码用 bcrypt（pip install bcrypt）哈希存储，避免明文 / MD5
    - Flask Session（cookie 签名）保持登录态；session['user_id'] 即可
    - current_user() 读 session + DB，返回 user dict 或 None
    - @require_user：未登录返 401 + JSON
    - @require_kb_permission(level='read')：未登录 401 / 无权限 403

兼容：
    - 现有 ADMIN_TOKEN（X-Admin-Token header）保留为运维通道
      装饰器优先校验 admin token；若 admin token 无效再走 session 校验
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, Optional

import bcrypt
from flask import g, jsonify, request, session

from custom_app.db import new_id, now_iso
from custom_app.repositories import UserRepository

logger = logging.getLogger(__name__)

SESSION_USER_ID_KEY = "user_id"


# ---------------------------------------------------------------------------
# 密码哈希
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    """bcrypt 哈希；返回 utf-8 字符串供 DB 存储。"""
    if not plain:
        raise ValueError("password must not be empty")
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, password_hash: str) -> bool:
    """常量时间比对。无效哈希返 False（不抛）。"""
    if not plain or not password_hash:
        return False
    try:
        return bcrypt.checkpw(
            plain.encode("utf-8"), password_hash.encode("utf-8"),
        )
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# 创建用户
# ---------------------------------------------------------------------------


def create_user(
    *,
    username: str,
    password: str,
    display_name: str = "",
    user_id: Optional[str] = None,
) -> str:
    """创建用户；username 已存在抛 ValueError。返回 user_id。"""
    username = (username or "").strip()
    if not username:
        raise ValueError("username required")
    if not password:
        raise ValueError("password required")

    repo = UserRepository()
    if repo.exists_username(username):
        raise ValueError(f"username already exists: {username}")

    uid = user_id or new_id("user")
    repo.create(
        user_id=uid,
        username=username,
        password_hash=hash_password(password),
        display_name=display_name or username,
        created_at=now_iso(),
    )
    logger.info("user created: id=%s username=%s", uid, username)
    return uid


# ---------------------------------------------------------------------------
# 认证 + Session
# ---------------------------------------------------------------------------


def authenticate(username: str, password: str) -> Optional[dict[str, Any]]:
    """用户名 + 密码验证；返回 user dict 或 None。

    成功时更新 last_login_at。失败原因不区分（避免枚举攻击）。
    """
    if not username or not password:
        return None
    repo = UserRepository()
    user = repo.find_by_username(username)
    if not user:
        return None
    if (user.get("status") or "active") != "active":
        return None  # 禁用账号
    if not verify_password(password, str(user.get("password_hash") or "")):
        return None
    # 成功：刷 last_login_at
    repo.update_last_login(str(user["user_id"]), when_iso=now_iso())
    return user


def login_user(user_id: str) -> None:
    """把 user_id 写进 Flask session。"""
    session[SESSION_USER_ID_KEY] = user_id
    session.permanent = True  # 用 PERMANENT_SESSION_LIFETIME 控制时长


def logout_user() -> None:
    session.pop(SESSION_USER_ID_KEY, None)


def current_user_id() -> Optional[str]:
    """直接从 session 取 user_id；不查 DB。"""
    uid = session.get(SESSION_USER_ID_KEY)
    return str(uid) if uid else None


def current_user() -> Optional[dict[str, Any]]:
    """取 session 中 user_id 对应的活跃用户 dict；按 session.user_id 缓存到 g。

    缓存键含 user_id，避免"未登录时缓存了 None → 同一 request 内 login_user
    之后还误返 None"。
    """
    uid = current_user_id()
    cached_uid = getattr(g, "_perm_current_user_uid", None)
    cached_user = getattr(g, "_perm_current_user", None)
    if cached_uid == uid and cached_user is not None:
        return cached_user if cached_user else None

    if not uid:
        g._perm_current_user_uid = None
        g._perm_current_user = False
        return None
    user = UserRepository().find_by_id(uid)
    if not user or (user.get("status") or "active") != "active":
        # 用户被禁/删 → 清掉 session
        logout_user()
        g._perm_current_user_uid = None
        g._perm_current_user = False
        return None
    g._perm_current_user_uid = uid
    g._perm_current_user = user
    return user


# ---------------------------------------------------------------------------
# Admin token bypass：兼容现有 ULTRARAG_ADMIN_TOKEN 运维通道
# ---------------------------------------------------------------------------


def _is_valid_admin_token() -> bool:
    """读 X-Admin-Token header 或 cookie，与 env 配置的 ADMIN_TOKEN 对比。

    复用 app.py 已有的工具函数避免重复。
    """
    try:
        from custom_app.app import (
            _get_admin_token_from_request,
            get_configured_admin_token,
        )
    except Exception:  # noqa: BLE001
        return False
    configured = get_configured_admin_token()
    if not configured:
        return False
    presented = _get_admin_token_from_request()
    return bool(presented) and presented == configured


# ---------------------------------------------------------------------------
# 装饰器：require_user / require_kb_permission
# ---------------------------------------------------------------------------


def _err(msg: str, code: str, status: int):
    return jsonify({"error": msg, "code": code}), status


def require_user(view: Callable) -> Callable:
    """要求用户已登录或带有效 admin token。"""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if _is_valid_admin_token():
            return view(*args, **kwargs)
        if current_user() is None:
            return _err("login required", "AUTH_REQUIRED", 401)
        return view(*args, **kwargs)

    return wrapped


def require_kb_permission(level: str = "read") -> Callable:
    """要求登录用户对目标 KB 有 read/write/admin 权限。

    使用方式：
        @bp.route('/api/kb/<kb_id>/...', methods=['POST'])
        @require_kb_permission('write')
        def handler(kb_id): ...

    优先级：
        1. 有效 admin token → 直接通过
        2. 未登录 → 401
        3. 登录但无 KB 权限 → 403
    """

    def decorator(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if _is_valid_admin_token():
                return view(*args, **kwargs)
            user = current_user()
            if user is None:
                return _err("login required", "AUTH_REQUIRED", 401)
            kb_id = kwargs.get("kb_id") or request.view_args.get("kb_id") \
                if request.view_args else None
            if not kb_id:
                # 装饰器用错了；默认放行避免破坏现有 API
                logger.warning(
                    "require_kb_permission used on route without kb_id arg",
                )
                return view(*args, **kwargs)
            ok = UserRepository().user_has_kb_permission(
                user_id=str(user["user_id"]), kb_id=str(kb_id),
                min_level=level,
            )
            if not ok:
                return _err(
                    f"no {level} permission for kb={kb_id}",
                    "KB_FORBIDDEN", 403,
                )
            return view(*args, **kwargs)

        return wrapped

    return decorator
