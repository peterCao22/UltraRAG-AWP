"""Phase 7: 对话模型 Provider 元信息注册表（代码常量，不入库）。

MVP 4 个 provider：
    gemini             —— Google Gemini（默认 generativelanguage.googleapis.com）
    openai             —— OpenAI 官方
    anthropic          —— Anthropic Claude
    openai_compatible  —— OpenAI 兼容（vLLM / 自部署 Qwen 等，需手填 base_url）
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderMeta:
    name: str
    label: str
    default_base_url: str
    requires_auth: bool
    example_model_name: str


PROVIDERS: dict[str, ProviderMeta] = {
    "gemini": ProviderMeta(
        name="gemini",
        label="Google Gemini",
        # Phase 7.1: 走 Google 官方 OpenAI 兼容端点，避免单独维护 Gemini 原生协议
        default_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        requires_auth=True,
        example_model_name="gemini-2.5-pro",
    ),
    "openai": ProviderMeta(
        name="openai",
        label="OpenAI",
        default_base_url="https://api.openai.com/v1",
        requires_auth=True,
        example_model_name="gpt-4o",
    ),
    "anthropic": ProviderMeta(
        name="anthropic",
        label="Anthropic Claude",
        default_base_url="https://api.anthropic.com",
        requires_auth=True,
        example_model_name="claude-haiku-4-5-20251001",
    ),
    "openai_compatible": ProviderMeta(
        name="openai_compatible",
        label="OpenAI 兼容（vLLM / 自部署）",
        default_base_url="",
        requires_auth=False,
        example_model_name="Qwen2.5-7B-Instruct",
    ),
}


def effective_base_url(provider: str, user_base_url: str = "") -> str:
    """根据 provider 返回真正调用时的 base_url。

    用户在 admin 里没填 base_url 时回退到 provider 默认值；
    显式填了的优先使用。openai_compatible 没默认值，必须用户填。
    """
    user_url = (user_base_url or "").strip().rstrip("/")
    if user_url:
        return user_url
    if provider not in PROVIDERS:
        raise UnknownProvider(provider)
    return PROVIDERS[provider].default_base_url.rstrip("/")


class UnknownProvider(ValueError):
    pass


def is_valid_provider(name: str) -> bool:
    return name in PROVIDERS


def list_providers() -> list[dict]:
    """供 GET /api/admin/models/providers 直接返回。"""
    return [
        {
            "name": p.name,
            "label": p.label,
            "default_base_url": p.default_base_url,
            "requires_auth": p.requires_auth,
            "example_model_name": p.example_model_name,
        }
        for p in PROVIDERS.values()
    ]


def provider_label(name: str) -> str:
    if name not in PROVIDERS:
        raise UnknownProvider(name)
    return PROVIDERS[name].label


def provider_default_base_url(name: str) -> str:
    if name not in PROVIDERS:
        raise UnknownProvider(name)
    return PROVIDERS[name].default_base_url


def provider_requires_auth(name: str) -> bool:
    if name not in PROVIDERS:
        raise UnknownProvider(name)
    return PROVIDERS[name].requires_auth
