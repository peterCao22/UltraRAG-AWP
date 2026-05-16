"""Phase 7: Admin 端对话模型 CRUD + 测试连接 API。

路由前缀：/api/admin/models
鉴权由 app.py 中的 ULTRARAG_ADMIN_TOKEN 中间件统一处理（属于 /admin 范畴）。

API Key 处理约定：
    - GET 列表 / GET 单条：返回里把 api_key 屏蔽为 '***'（有值才显示）
    - POST 创建：必须带 api_key（openai_compatible 可空字符串）
    - PUT 更新：api_key 为空字符串 = 不变；非空 = 覆盖
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from flask import Blueprint, jsonify, request

from custom_app.db import new_id, now_iso
from custom_app.repositories import ChatModelRepository
from custom_app.services.chat_adapter_factory import resolve_test_adapter
from custom_app.services.providers import (
    is_valid_provider,
    list_providers,
    provider_requires_auth,
)
from custom_app.utils.ssrf_guard import SSRFRejected, validate_url_for_ssrf

logger = logging.getLogger(__name__)

admin_models_bp = Blueprint("admin_models_api", __name__)


# ── helpers ──────────────────────────────────────────────────────────────────

def _req_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"


def _ok(data: Any):
    return jsonify({"request_id": _req_id(), "data": data})


def _err(msg: str, code: str, status: int):
    return jsonify({"request_id": _req_id(), "error": msg, "code": code}), status


def _hide_sensitive(row: dict[str, Any]) -> dict[str, Any]:
    """把 api_key 屏蔽为 '***'（仅有值时；空字符串保持空）。"""
    out = dict(row)
    if out.get("api_key"):
        out["api_key"] = "***"
    # 内部字段不返回前端
    out.pop("id", None)
    return out


def _validate_create_payload(body: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """返回 (cleaned, error_msg)。"""
    name = str(body.get("name", "")).strip()
    provider = str(body.get("provider", "")).strip()
    model_name = str(body.get("model_name", "")).strip()
    base_url = str(body.get("base_url", "")).strip()
    api_key = str(body.get("api_key", ""))
    description = str(body.get("description", ""))
    enabled = bool(body.get("enabled", True))
    is_default = bool(body.get("is_default", False))
    extra = body.get("extra") or {}

    if not name:
        return {}, "name is required"
    if not provider:
        return {}, "provider is required"
    if not is_valid_provider(provider):
        return {}, f"invalid provider: {provider!r}"
    if not model_name:
        return {}, "model_name is required"
    if provider_requires_auth(provider) and not api_key:
        return {}, f"api_key is required for provider {provider!r}"
    if not isinstance(extra, dict):
        return {}, "extra must be an object"

    try:
        validate_url_for_ssrf(base_url)
    except SSRFRejected as exc:
        return {}, f"base_url rejected by SSRF guard: {exc}"

    return {
        "name": name,
        "provider": provider,
        "model_name": model_name,
        "base_url": base_url,
        "api_key": api_key,
        "description": description,
        "enabled": enabled,
        "is_default": is_default,
        "extra": extra,
    }, None


# ── routes ───────────────────────────────────────────────────────────────────

@admin_models_bp.route("/api/admin/models/providers", methods=["GET"])
def get_providers():
    """返回 4 个 provider 元信息（label / default_base_url / requires_auth）。"""
    return _ok(list_providers())


@admin_models_bp.route("/api/admin/models", methods=["GET"])
def list_models():
    """全量列表（含 disabled，不含已软删）；api_key 屏蔽。"""
    rows = ChatModelRepository().list_active(include_disabled=True)
    return _ok([_hide_sensitive(r) for r in rows])


@admin_models_bp.route("/api/admin/models/<string:model_id>", methods=["GET"])
def get_model(model_id: str):
    row = ChatModelRepository().get(model_id)
    if row is None:
        return _err(f"model not found: {model_id}", "MODEL_NOT_FOUND", 404)
    return _ok(_hide_sensitive(row))


@admin_models_bp.route("/api/admin/models", methods=["POST"])
def create_model():
    body = request.get_json(silent=True) or {}
    cleaned, err = _validate_create_payload(body)
    if err:
        return _err(err, "INVALID_PAYLOAD", 400)

    repo = ChatModelRepository()
    model_id = new_id("model")
    now = now_iso()
    repo.create(
        model_id=model_id,
        name=cleaned["name"],
        provider=cleaned["provider"],
        model_name=cleaned["model_name"],
        base_url=cleaned["base_url"],
        api_key=cleaned["api_key"],
        is_default=False,  # 不在创建时设默认；用 set-default 路由
        enabled=cleaned["enabled"],
        description=cleaned["description"],
        extra=cleaned["extra"],
        created_at=now,
    )

    if cleaned["is_default"]:
        repo.set_default(model_id, updated_at=now)

    row = repo.get(model_id)
    return _ok(_hide_sensitive(row))


@admin_models_bp.route("/api/admin/models/<string:model_id>", methods=["PUT"])
def update_model(model_id: str):
    repo = ChatModelRepository()
    if repo.get(model_id) is None:
        return _err(f"model not found: {model_id}", "MODEL_NOT_FOUND", 404)

    body = request.get_json(silent=True) or {}
    # 局部更新：仅传入字段会被改
    update_kwargs: dict[str, Any] = {}
    for field in ("name", "provider", "model_name", "description"):
        if field in body:
            update_kwargs[field] = str(body[field]).strip()
    if "base_url" in body:
        url = str(body["base_url"]).strip()
        try:
            validate_url_for_ssrf(url)
        except SSRFRejected as exc:
            return _err(
                f"base_url rejected by SSRF guard: {exc}",
                "INVALID_PAYLOAD", 400,
            )
        update_kwargs["base_url"] = url
    if "api_key" in body:
        ak = str(body["api_key"])
        # 空字符串视为 "不变"，与 admin UI 「留空表示保留原 key」语义一致
        if ak:
            update_kwargs["api_key"] = ak
    if "enabled" in body:
        update_kwargs["enabled"] = bool(body["enabled"])
    if "extra" in body and isinstance(body["extra"], dict):
        update_kwargs["extra"] = body["extra"]

    # 验证 provider（若有改动）
    if "provider" in update_kwargs and not is_valid_provider(update_kwargs["provider"]):
        return _err(
            f"invalid provider: {update_kwargs['provider']!r}",
            "INVALID_PAYLOAD", 400,
        )

    repo.update(model_id, updated_at=now_iso(), **update_kwargs)
    return _ok(_hide_sensitive(repo.get(model_id)))


@admin_models_bp.route("/api/admin/models/<string:model_id>", methods=["DELETE"])
def delete_model(model_id: str):
    repo = ChatModelRepository()
    if repo.get(model_id) is None:
        return _err(f"model not found: {model_id}", "MODEL_NOT_FOUND", 404)
    repo.soft_delete(model_id, deleted_at=now_iso())
    return _ok({"model_id": model_id, "deleted": True})


@admin_models_bp.route(
    "/api/admin/models/<string:model_id>/set-default", methods=["POST"]
)
def set_default_model(model_id: str):
    repo = ChatModelRepository()
    if repo.get(model_id) is None:
        return _err(f"model not found: {model_id}", "MODEL_NOT_FOUND", 404)
    repo.set_default(model_id, updated_at=now_iso())
    return _ok(_hide_sensitive(repo.get(model_id)))


@admin_models_bp.route(
    "/api/admin/models/<string:model_id>/test", methods=["POST"]
)
def test_model(model_id: str):
    """真实发短 prompt 验证连接。消耗 1 次 token；UI 应防抖。"""
    repo = ChatModelRepository()
    row = repo.get(model_id)
    if row is None:
        return _err(f"model not found: {model_id}", "MODEL_NOT_FOUND", 404)
    try:
        adapter = resolve_test_adapter(row)
    except ValueError as exc:
        return _err(str(exc), "INVALID_PROVIDER", 400)
    result = adapter.test_ping()
    return _ok(result)
