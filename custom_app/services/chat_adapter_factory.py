"""Phase 7.1: 根据 chat_models 行返回对应的 LLM 适配器。

调用约定：
    - resolve_test_adapter(model_row) 返回 admin "测试连接" 用的轻量适配器
    - resolve_chat_adapter(model_row) 返回真正接对话链路的 LLMAdapter（满足 Protocol）

策略（Phase 7.1）：
    - gemini / openai / openai_compatible → OpenAICompatAdapter（base_url 解析负责
      把 gemini 路由到 Google 官方 OpenAI 兼容端点）
    - anthropic → AnthropicAdapter（专用 SDK，独立协议转换）

老旧 GeminiTestAdapter 仍可用于"测试连接"——但 chat 链路用 OpenAICompatAdapter。
"""

from __future__ import annotations

from typing import Any

from custom_app.services.providers import (
    effective_base_url,
    is_valid_provider,
)
from custom_app.services.providers.llm_protocol import LLMAdapter


class UnsupportedProviderForChat(RuntimeError):
    """provider 配置错误或暂不支持对话链路。"""


def _extract_common(model_row: dict[str, Any]) -> dict[str, Any]:
    provider = (model_row.get("provider") or "").strip()
    if not is_valid_provider(provider):
        raise UnsupportedProviderForChat(f"unknown provider: {provider!r}")
    extra = model_row.get("extra") or {}
    return {
        "provider": provider,
        "api_key": model_row.get("api_key") or "",
        "model": model_row.get("model_name") or "",
        "base_url": effective_base_url(provider, model_row.get("base_url") or ""),
        "extra": extra,
    }


def resolve_chat_adapter(model_row: dict[str, Any]) -> LLMAdapter:
    """返回真正接对话链路的 LLMAdapter。"""
    cfg = _extract_common(model_row)
    provider = cfg["provider"]
    if provider in ("gemini", "openai", "openai_compatible"):
        from custom_app.services.providers.openai_compat_adapter import (
            OpenAICompatAdapter,
        )
        return OpenAICompatAdapter(
            api_key=cfg["api_key"], model=cfg["model"],
            base_url=cfg["base_url"], extra=cfg["extra"],
        )
    if provider == "anthropic":
        from custom_app.services.providers.anthropic_adapter import AnthropicAdapter
        return AnthropicAdapter(
            api_key=cfg["api_key"], model=cfg["model"],
            base_url=cfg["base_url"], extra=cfg["extra"],
        )
    raise UnsupportedProviderForChat(f"unhandled provider: {provider!r}")


def resolve_test_adapter(model_row: dict[str, Any]):
    """admin "测试连接" 用；返回有 test_ping() 方法的轻量适配器。

    Phase 7.1：4 个 provider 全部走完整 LLMAdapter（OpenAICompatAdapter /
    AnthropicAdapter）的 test_ping。不再用早期的 GeminiTestAdapter。
    """
    return resolve_chat_adapter(model_row)


# ─── 兼容 Phase 7 旧调用 ──────────────────────────────────────────────


def resolve_chat_adapter_for_runner(model_row: dict[str, Any]) -> LLMAdapter:
    """Deprecated alias，等价于 resolve_chat_adapter；保留以免老调用方报错。"""
    return resolve_chat_adapter(model_row)
