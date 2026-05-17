"""Phase 7.2.A: Admin 端 Agent 配置 CRUD API。

路由前缀：/api/admin/agents
鉴权：与 admin_models 一样由 app.py 的 ULTRARAG_ADMIN_TOKEN 中间件统一处理。

业务规则：
    - builtin agent（is_builtin=True）可改 prompt 等字段，但不可改 agent_mode / is_builtin
    - builtin agent 不可删除（DELETE 返 400）
    - agent_id 由后端生成（new_id("agent")），前端不能指定
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from flask import Blueprint, jsonify, request

from custom_app.db import new_id, now_iso
from custom_app.repositories import ChatAgentRepository

logger = logging.getLogger(__name__)

admin_agents_bp = Blueprint("admin_agents_api", __name__)


_VALID_AGENT_MODES = ("quick", "agent")


def _req_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"


def _ok(data: Any):
    return jsonify({"request_id": _req_id(), "data": data})


def _err(msg: str, code: str, status: int):
    return jsonify({"request_id": _req_id(), "error": msg, "code": code}), status


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    """admin GET 返回时去掉内部 id；prompt 等明文字段保留。"""
    out = dict(row)
    out.pop("id", None)
    return out


def _validate_create_payload(
    body: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    name = str(body.get("name", "")).strip()
    agent_mode = str(body.get("agent_mode", "")).strip().lower()
    if not name:
        return {}, "name is required"
    if agent_mode not in _VALID_AGENT_MODES:
        return {}, f"agent_mode must be one of {_VALID_AGENT_MODES}"

    try:
        temperature = float(body.get("temperature", 0.7))
    except (TypeError, ValueError):
        return {}, "temperature must be a number"
    if not (0.0 <= temperature <= 2.0):
        return {}, "temperature out of range [0.0, 2.0]"

    try:
        max_tokens = int(body.get("max_tokens", 4096))
    except (TypeError, ValueError):
        return {}, "max_tokens must be an integer"
    if not (1 <= max_tokens <= 200_000):
        return {}, "max_tokens out of range [1, 200000]"

    return {
        "name": name,
        "agent_mode": agent_mode,
        "description": str(body.get("description", "")),
        "avatar": str(body.get("avatar", "")),
        "system_prompt": str(body.get("system_prompt", "")),
        "agent_system_prompt": str(body.get("agent_system_prompt", "")),
        "model_id": str(body.get("model_id", "")).strip(),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "enabled": bool(body.get("enabled", True)),
    }, None


@admin_agents_bp.route("/api/admin/agents", methods=["GET"])
def list_agents():
    """全量列表（含 disabled，不含已软删）；按 is_builtin DESC, created_at ASC 排序。"""
    rows = ChatAgentRepository().list_active(include_disabled=True)
    return _ok([_public_row(r) for r in rows])


@admin_agents_bp.route("/api/admin/agents/<string:agent_id>", methods=["GET"])
def get_agent(agent_id: str):
    row = ChatAgentRepository().get(agent_id)
    if row is None:
        return _err(f"agent not found: {agent_id}", "AGENT_NOT_FOUND", 404)
    return _ok(_public_row(row))


@admin_agents_bp.route("/api/admin/agents", methods=["POST"])
def create_agent():
    body = request.get_json(silent=True) or {}
    cleaned, err = _validate_create_payload(body)
    if err:
        return _err(err, "INVALID_PAYLOAD", 400)

    repo = ChatAgentRepository()
    agent_id = new_id("agent")
    now = now_iso()
    repo.create(
        agent_id=agent_id,
        name=cleaned["name"],
        agent_mode=cleaned["agent_mode"],
        description=cleaned["description"],
        avatar=cleaned["avatar"],
        system_prompt=cleaned["system_prompt"],
        agent_system_prompt=cleaned["agent_system_prompt"],
        model_id=cleaned["model_id"],
        temperature=cleaned["temperature"],
        max_tokens=cleaned["max_tokens"],
        enabled=cleaned["enabled"],
        is_builtin=False,  # 用户创建的从不 builtin
        created_at=now,
    )
    return _ok(_public_row(repo.get(agent_id)))


@admin_agents_bp.route("/api/admin/agents/<string:agent_id>", methods=["PUT"])
def update_agent(agent_id: str):
    repo = ChatAgentRepository()
    existing = repo.get(agent_id)
    if existing is None:
        return _err(f"agent not found: {agent_id}", "AGENT_NOT_FOUND", 404)

    body = request.get_json(silent=True) or {}

    # agent_mode / is_builtin / tenant_id 不允许改（与 §五.6 文档一致）
    if "agent_mode" in body:
        if str(body["agent_mode"]).strip().lower() != existing["agent_mode"]:
            return _err(
                "agent_mode is immutable after creation",
                "INVALID_PAYLOAD", 400,
            )
    if "is_builtin" in body and bool(body["is_builtin"]) != bool(
        existing.get("is_builtin", False)
    ):
        return _err(
            "is_builtin is immutable after creation",
            "INVALID_PAYLOAD", 400,
        )

    update_kwargs: dict[str, Any] = {}
    for field in ("name", "description", "avatar"):
        if field in body:
            update_kwargs[field] = str(body[field])
    for field in ("system_prompt", "agent_system_prompt"):
        if field in body:
            update_kwargs[field] = str(body[field])
    if "model_id" in body:
        update_kwargs["model_id"] = str(body["model_id"]).strip()
    if "temperature" in body:
        try:
            t = float(body["temperature"])
        except (TypeError, ValueError):
            return _err("temperature must be a number", "INVALID_PAYLOAD", 400)
        if not (0.0 <= t <= 2.0):
            return _err(
                "temperature out of range [0.0, 2.0]",
                "INVALID_PAYLOAD", 400,
            )
        update_kwargs["temperature"] = t
    if "max_tokens" in body:
        try:
            mt = int(body["max_tokens"])
        except (TypeError, ValueError):
            return _err("max_tokens must be an integer", "INVALID_PAYLOAD", 400)
        if not (1 <= mt <= 200_000):
            return _err(
                "max_tokens out of range [1, 200000]",
                "INVALID_PAYLOAD", 400,
            )
        update_kwargs["max_tokens"] = mt
    if "enabled" in body:
        update_kwargs["enabled"] = bool(body["enabled"])

    if not update_kwargs:
        # 无可更新字段；直接返回当前行
        return _ok(_public_row(existing))

    if not update_kwargs.get("name", existing.get("name")):
        return _err("name must not be empty", "INVALID_PAYLOAD", 400)

    repo.update(agent_id, updated_at=now_iso(), **update_kwargs)
    return _ok(_public_row(repo.get(agent_id)))


@admin_agents_bp.route("/api/admin/agents/<string:agent_id>", methods=["DELETE"])
def delete_agent(agent_id: str):
    repo = ChatAgentRepository()
    existing = repo.get(agent_id)
    if existing is None:
        return _err(f"agent not found: {agent_id}", "AGENT_NOT_FOUND", 404)
    if bool(existing.get("is_builtin", False)):
        return _err(
            "builtin agent cannot be deleted",
            "BUILTIN_IMMUTABLE", 400,
        )
    repo.soft_delete(agent_id, deleted_at=now_iso())
    return _ok({"agent_id": agent_id, "deleted": True})
