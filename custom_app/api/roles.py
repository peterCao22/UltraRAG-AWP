import uuid

from flask import Blueprint, jsonify, request

from custom_app.db import new_id, now_iso
from custom_app.repositories import KbRepository, RoleRepository

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

    role_repo = RoleRepository()
    if role_repo.find_by_name(name):
        return _err(f"role name already exists: {name}", "ROLE_NAME_EXISTS", 409)

    role_id = new_id("role")
    role_repo.create(
        role_id=role_id, name=name, description=description,
        created_at=now_iso(),
    )
    return _ok({"role_id": role_id, "name": name, "description": description})


@roles_bp.route("/api/roles", methods=["GET"])
def list_roles():
    rows = RoleRepository().list_all()
    return _ok(rows)


@roles_bp.route("/api/roles/<string:role_id>", methods=["GET"])
def get_role(role_id: str):
    item = RoleRepository().find_by_id(role_id)
    if item is None:
        return _err(f"role not found: {role_id}", "ROLE_NOT_FOUND", 404)
    return _ok(item)


@roles_bp.route("/api/roles/<string:role_id>", methods=["DELETE"])
def delete_role(role_id: str):
    role_repo = RoleRepository()
    if not role_repo.exists(role_id):
        return _err(f"role not found: {role_id}", "ROLE_NOT_FOUND", 404)
    role_repo.delete(role_id)
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

    role_repo = RoleRepository()
    if not role_repo.exists(role_id):
        return _err(f"role not found: {role_id}", "ROLE_NOT_FOUND", 404)
    if not KbRepository().exists(kb_id):
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)

    role_repo.upsert_permission(
        role_id=role_id, kb_id=kb_id, access_level=access_level,
        updated_at=now_iso(),
    )
    return _ok({"role_id": role_id, "kb_id": kb_id, "access_level": access_level})


@roles_bp.route("/api/roles/<string:role_id>/permissions", methods=["GET"])
def list_role_permissions(role_id: str):
    role_repo = RoleRepository()
    if not role_repo.exists(role_id):
        return _err(f"role not found: {role_id}", "ROLE_NOT_FOUND", 404)
    rows = role_repo.list_permissions(role_id)
    return _ok(rows)


@roles_bp.route("/api/roles/<string:role_id>/permissions/<string:kb_id>", methods=["DELETE"])
def revoke_kb_permission(role_id: str, kb_id: str):
    role_repo = RoleRepository()
    # list_permissions 不直接给 find_one 接口；用 list 然后过滤（数据量小可接受）
    # 或者直接调 delete_permission 并由 SQL 决定是否有受影响行
    # 简化：直接 delete（幂等），但保持 404 行为需要先查存在性
    perms = role_repo.list_permissions(role_id)
    has_perm = any(p["kb_id"] == kb_id for p in perms)
    if not has_perm:
        return _err(
            f"permission not found: role={role_id} kb={kb_id}",
            "PERMISSION_NOT_FOUND", 404,
        )
    role_repo.delete_permission(role_id, kb_id)
    return _ok({"role_id": role_id, "kb_id": kb_id, "revoked": True})
