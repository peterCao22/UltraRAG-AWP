"""Phase 7: Provider 注册表 + SSRF guard 单测。"""

from __future__ import annotations

import pytest

from custom_app.services.providers import (
    PROVIDERS,
    UnknownProvider,
    is_valid_provider,
    list_providers,
    provider_default_base_url,
    provider_requires_auth,
)
from custom_app.utils.ssrf_guard import SSRFRejected, validate_url_for_ssrf


class TestProviderRegistry:
    def test_has_four_mvp_providers(self):
        assert set(PROVIDERS.keys()) == {
            "gemini", "openai", "anthropic", "openai_compatible",
        }

    def test_list_providers_returns_serializable_dicts(self):
        items = list_providers()
        assert len(items) == 4
        for item in items:
            assert "name" in item and "label" in item
            assert "default_base_url" in item
            assert "requires_auth" in item
            assert "example_model_name" in item

    def test_is_valid(self):
        assert is_valid_provider("gemini")
        assert not is_valid_provider("bogus")

    def test_default_base_url(self):
        assert "googleapis.com" in provider_default_base_url("gemini")
        # openai_compatible 没有默认（用户必填）
        assert provider_default_base_url("openai_compatible") == ""

    def test_unknown_raises(self):
        with pytest.raises(UnknownProvider):
            provider_default_base_url("bogus")

    def test_requires_auth(self):
        assert provider_requires_auth("gemini") is True
        assert provider_requires_auth("openai_compatible") is False


class TestSSRF:
    def test_empty_url_passes(self):
        validate_url_for_ssrf("")
        validate_url_for_ssrf("   ")

    def test_http_https_pass(self):
        validate_url_for_ssrf("https://api.openai.com/v1")
        validate_url_for_ssrf("http://192.168.8.40:8000")  # 默认允许私网

    def test_rejects_non_http_scheme(self):
        with pytest.raises(SSRFRejected):
            validate_url_for_ssrf("ftp://example.com")
        with pytest.raises(SSRFRejected):
            validate_url_for_ssrf("file:///etc/passwd")

    def test_rejects_cloud_metadata_endpoints(self):
        with pytest.raises(SSRFRejected):
            validate_url_for_ssrf("http://169.254.169.254/latest/meta-data/")
        with pytest.raises(SSRFRejected):
            validate_url_for_ssrf("http://metadata.google.internal/")

    def test_strict_mode_blocks_private(self, monkeypatch):
        monkeypatch.setenv("ULTRARAG_BLOCK_PRIVATE_BASE_URL", "1")
        with pytest.raises(SSRFRejected):
            validate_url_for_ssrf("http://192.168.8.40:8000")
        with pytest.raises(SSRFRejected):
            validate_url_for_ssrf("http://127.0.0.1:8000")

    def test_missing_hostname_rejected(self):
        with pytest.raises(SSRFRejected):
            validate_url_for_ssrf("http://")
