"""Phase 1：会话 CRUD API（与 chat 流式落库配合）。"""

from flask import Blueprint, jsonify, request

from custom_app.services import session_store

sessions_bp = Blueprint("sessions_api", __name__)


@sessions_bp.route("/api/sessions", methods=["POST"])
def create_session():
    data = request.get_json(silent=True) or {}
    kb_id = str(data.get("kb_id", "")).strip()
    if not kb_id:
        return jsonify({"success": False, "error": "kb_id 不能为空"}), 400
    title = str(data.get("title", "")).strip()
    agent_mode = str(data.get("agent_mode", "quick")).strip().lower()
    row = session_store.create_session(kb_id, title=title, agent_mode=agent_mode)
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
    rows = session_store.list_sessions_for_kb(kb_id, limit=limit)
    return jsonify({"success": True, "data": {"items": rows}})


@sessions_bp.route("/api/sessions/<session_id>", methods=["GET"])
def get_one(session_id: str):
    sid = (session_id or "").strip()
    if not sid:
        return jsonify({"success": False, "error": "session_id 无效"}), 400
    row = session_store.get_session(sid)
    if not row:
        return jsonify({"success": False, "error": "not_found"}), 404
    return jsonify({"success": True, "data": row})


@sessions_bp.route("/api/sessions/<session_id>", methods=["PATCH"])
def patch_session(session_id: str):
    sid = (session_id or "").strip()
    data = request.get_json(silent=True) or {}
    title = str(data.get("title", "")).strip()
    if not sid or not title:
        return jsonify({"success": False, "error": "title 不能为空"}), 400
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
    items = session_store.list_messages(sid)
    return jsonify({"success": True, "data": {"items": items}})
