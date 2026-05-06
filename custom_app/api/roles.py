import uuid

from flask import Blueprint, jsonify, request

from custom_app.db import get_conn, new_id, now_iso, row_to_dict

roles_bp = Blueprint("roles_api", __name__)


def _req_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"


def _ok(data):
    return jsonify({"request_id": _req_id(), "data": data})


def _err(msg: str, code: str, status: int):
    return jsonify({"request_id": _req_id(), "error": msg, "code": code}), status


@roles_bp.route("/api/roles", methods=["POST"])
def create_role():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()
    description = str(body.get("description", "")).strip()

    if not name:
        return _err("name is required", "ROLE_NAME_REQUIRED", 400)

    role_id = new_id("role")
    now = now_iso()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT role_id FROM roles WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            return _err(f"role name already exists: {name}", "ROLE_NAME_EXISTS", 409)
        conn.execute(
            """INSERT INTO roles (role_id, name, description, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (role_id, name, description, now, now),
        )
    return _ok({"role_id": role_id, "name": name, "description": description})


@roles_bp.route("/api/roles", methods=["GET"])
def list_roles():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role_id, name, description, created_at, updated_at FROM roles ORDER BY created_at DESC"
        ).fetchall()
    return _ok([row_to_dict(r) for r in rows])


@roles_bp.route("/api/roles/<string:role_id>", methods=["GET"])
def get_role(role_id: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM roles WHERE role_id = ?", (role_id,)
        ).fetchone()
    item = row_to_dict(row)
    if item is None:
        return _err(f"role not found: {role_id}", "ROLE_NOT_FOUND", 404)
    return _ok(item)


@roles_bp.route("/api/roles/<string:role_id>", methods=["DELETE"])
def delete_role(role_id: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT role_id FROM roles WHERE role_id = ?", (role_id,)
        ).fetchone()
        if row is None:
            return _err(f"role not found: {role_id}", "ROLE_NOT_FOUND", 404)
        conn.execute("DELETE FROM role_kb_permissions WHERE role_id = ?", (role_id,))
        conn.execute("DELETE FROM roles WHERE role_id = ?", (role_id,))
    return _ok({"role_id": role_id, "deleted": True})


@roles_bp.route("/api/roles/<string:role_id>/permissions", methods=["POST"])
def assign_kb_permission(role_id: str):
    body = request.get_json(silent=True) or {}
    kb_id = str(body.get("kb_id", "")).strip()
    access_level = str(body.get("access_level", "read")).strip()

    if not kb_id:
        return _err("kb_id is required", "KB_ID_REQUIRED", 400)
    if access_level not in ("read", "write", "admin"):
        return _err("access_level must be read/write/admin", "INVALID_ACCESS_LEVEL", 400)

    with get_conn() as conn:
        role = conn.execute(
            "SELECT role_id FROM roles WHERE role_id = ?", (role_id,)
        ).fetchone()
        if role is None:
            return _err(f"role not found: {role_id}", "ROLE_NOT_FOUND", 404)

        kb = conn.execute(
            "SELECT kb_id FROM knowledge_bases WHERE kb_id = ?", (kb_id,)
        ).fetchone()
        if kb is None:
            return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)

        now = now_iso()
        conn.execute(
            """INSERT INTO role_kb_permissions (role_id, kb_id, access_level, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(role_id, kb_id) DO UPDATE SET
                 access_level = excluded.access_level,
                 updated_at = excluded.updated_at""",
            (role_id, kb_id, access_level, now, now),
        )
    return _ok({"role_id": role_id, "kb_id": kb_id, "access_level": access_level})


@roles_bp.route("/api/roles/<string:role_id>/permissions", methods=["GET"])
def list_role_permissions(role_id: str):
    with get_conn() as conn:
        role = conn.execute(
            "SELECT role_id FROM roles WHERE role_id = ?", (role_id,)
        ).fetchone()
        if role is None:
            return _err(f"role not found: {role_id}", "ROLE_NOT_FOUND", 404)

        rows = conn.execute(
            """SELECT p.role_id, p.kb_id, p.access_level, k.name as kb_name, p.created_at, p.updated_at
               FROM role_kb_permissions p
               LEFT JOIN knowledge_bases k ON k.kb_id = p.kb_id
               WHERE p.role_id = ?
               ORDER BY p.created_at DESC""",
            (role_id,),
        ).fetchall()
    return _ok([row_to_dict(r) for r in rows])


@roles_bp.route("/api/roles/<string:role_id>/permissions/<string:kb_id>", methods=["DELETE"])
def revoke_kb_permission(role_id: str, kb_id: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT role_id FROM role_kb_permissions WHERE role_id = ? AND kb_id = ?",
            (role_id, kb_id),
        ).fetchone()
        if row is None:
            return _err(f"permission not found: role={role_id} kb={kb_id}", "PERMISSION_NOT_FOUND", 404)
        conn.execute(
            "DELETE FROM role_kb_permissions WHERE role_id = ? AND kb_id = ?",
            (role_id, kb_id),
        )
    return _ok({"role_id": role_id, "kb_id": kb_id, "revoked": True})
