"""Phase 1：会话 CRUD API（与 chat 流式落库配合）。

P-Perm（用户权限层）：
    - 登录用户：所有读 / 写仅限自己创建的会话（user_id 过滤 / 校验）。
    - admin token：bypass 所有权校验（运维 / 后台脚本场景）。
    - 既有匿名会话（user_id 为空）：仅 admin token 可见 / 操作。
"""

from flask import Blueprint, jsonify, request

from custom_app.services import session_store
from custom_app.services.auth import current_user_id

sessions_bp = Blueprint("sessions_api", __name__)


def _is_auth_disabled() -> bool:
    """ULTRARAG_AUTH_REQUIRED=0/false/no/off 时视为关闭 P-Perm 校验。

    与 app.py 全局 before_request 一致；开发 / 老回归测试用。
    """
    import os
    val = os.environ.get("ULTRARAG_AUTH_REQUIRED", "1").strip().lower()
    return val in ("0", "false", "no", "off")


def _is_admin_bypass() -> bool:
    """判断当前请求是否凭 admin token bypass 权限校验。

    与 app.py 的全局 before_request 一致：仅当 ULTRARAG_ADMIN_TOKEN 配置且
    请求带 X-Admin-Token 头匹配时，视为 admin。
    或 ULTRARAG_AUTH_REQUIRED=0 时全局跳过（开发模式）。
    """
    if _is_auth_disabled():
        return True
    import hmac
    import os
    expected = os.getenv("ULTRARAG_ADMIN_TOKEN", "").strip()
    if not expected:
        return False
    presented = (request.headers.get("X-Admin-Token", "") or "").strip()
    return bool(presented) and hmac.compare_digest(presented, expected)


def _owner_or_admin_or_403(row: dict) -> tuple | None:
    """检查会话归属权；非 owner 且非 admin 时返回 403 Response。"""
    if _is_admin_bypass():
        return None
    uid = current_user_id() or ""
    row_uid = (row or {}).get("user_id") or ""
    if uid and row_uid == uid:
        return None
    return jsonify({"success": False, "error": "forbidden"}), 403


@sessions_bp.route("/api/sessions", methods=["POST"])
def create_session():
    data = request.get_json(silent=True) or {}
    kb_id = str(data.get("kb_id", "")).strip()
    if not kb_id:
        return jsonify({"success": False, "error": "kb_id 不能为空"}), 400
    # P-Perm C7：必须对该 kb 有 read 权限，否则不能开会话
    from custom_app.api.chat import _check_kb_access
    allowed, reason = _check_kb_access(kb_id, level="read")
    if not allowed:
        return jsonify({
            "success": False,
            "error": reason or "forbidden",
            "code": "KB_FORBIDDEN",
        }), 403
    title = str(data.get("title", "")).strip()
    agent_mode = str(data.get("agent_mode", "quick")).strip().lower()
    uid = "" if _is_admin_bypass() else (current_user_id() or "")
    row = session_store.create_session(
        kb_id, title=title, agent_mode=agent_mode, user_id=uid,
    )
    return jsonify({"success": True, "data": row})


@sessions_bp.route("/api/sessions", methods=["GET"])
def list_sessions():
    kb_id = str(request.args.get("kb_id", "")).strip()
    if not kb_id:
        return jsonify({"success": False, "error": "kb_id 查询参数必填"}), 400
    try:
        limit = int(request.args.get("limit", "100"))
    except (TypeError, ValueError):
        limit = 100
    if _is_admin_bypass():
        rows = session_store.list_sessions_for_kb(kb_id, limit=limit)
    else:
        uid = current_user_id() or ""
        rows = session_store.list_sessions_for_kb(
            kb_id, limit=limit, user_id=uid,
        )
    return jsonify({"success": True, "data": {"items": rows}})


@sessions_bp.route("/api/sessions/<session_id>", methods=["GET"])
def get_one(session_id: str):
    sid = (session_id or "").strip()
    if not sid:
        return jsonify({"success": False, "error": "session_id 无效"}), 400
    row = session_store.get_session(sid)
    if not row:
        return jsonify({"success": False, "error": "not_found"}), 404
    forbid = _owner_or_admin_or_403(row)
    if forbid:
        return forbid
    return jsonify({"success": True, "data": row})


@sessions_bp.route("/api/sessions/<session_id>", methods=["PATCH"])
def patch_session(session_id: str):
    sid = (session_id or "").strip()
    data = request.get_json(silent=True) or {}
    title = str(data.get("title", "")).strip()
    if not sid or not title:
        return jsonify({"success": False, "error": "title 不能为空"}), 400
    row = session_store.get_session(sid)
    if not row:
        return jsonify({"success": False, "error": "not_found"}), 404
    forbid = _owner_or_admin_or_403(row)
    if forbid:
        return forbid
    ok = session_store.update_session_title(sid, title)
    if not ok:
        return jsonify({"success": False, "error": "not_found"}), 404
    row = session_store.get_session(sid)
    return jsonify({"success": True, "data": row})


@sessions_bp.route("/api/sessions/<session_id>", methods=["DELETE"])
def delete_session(session_id: str):
    sid = (session_id or "").strip()
    if not sid:
        return jsonify({"success": False, "error": "session_id 无效"}), 400
    row = session_store.get_session(sid)
    if not row:
        return jsonify({"success": False, "error": "not_found"}), 404
    forbid = _owner_or_admin_or_403(row)
    if forbid:
        return forbid
    ok = session_store.delete_session(sid)
    if not ok:
        return jsonify({"success": False, "error": "not_found"}), 404
    return jsonify({"success": True, "data": {"session_id": sid}})


@sessions_bp.route("/api/sessions/<session_id>/messages", methods=["GET"])
def get_messages(session_id: str):
    sid = (session_id or "").strip()
    if not sid:
        return jsonify({"success": False, "error": "session_id 无效"}), 400
    row = session_store.get_session(sid)
    if not row:
        return jsonify({"success": False, "error": "not_found"}), 404
    forbid = _owner_or_admin_or_403(row)
    if forbid:
        return forbid
    items = session_store.list_messages(sid)
    return jsonify({"success": True, "data": {"items": items}})
