"""Phase 7: 对话模型 Provider 注册表。"""

from custom_app.services.providers.registry import (
    PROVIDERS,
    UnknownProvider,
    effective_base_url,
    is_valid_provider,
    list_providers,
    provider_default_base_url,
    provider_label,
    provider_requires_auth,
)

__all__ = [
    "PROVIDERS",
    "UnknownProvider",
    "effective_base_url",
    "is_valid_provider",
    "list_providers",
    "provider_default_base_url",
    "provider_label",
    "provider_requires_auth",
]
