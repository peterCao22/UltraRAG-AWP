"""Phase 7: Gemini test_ping 包装（用于 admin "测试连接"）。

复用现有 GeminiLLMAdapter 的 call() 接口；这里只暴露 test_ping，避免改动 llm_adapter.py
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class GeminiTestAdapter:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "",
        timeout: float = 30.0,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = (base_url or "").strip()
        self._timeout = float(timeout)
        self._extra = extra or {}

    def test_ping(self) -> dict[str, Any]:
        """同 OpenAI/Anthropic test_ping 接口；返回 {ok, latency_ms, error?, sample?}。

        实现：调 REST API 单次 generateContent，1 字 prompt。
        不依赖 GeminiLLMAdapter，因为它的 build_request_body 需要 messages_to_gemini_contents
        转换，比较绕；这里直接构造最小 body。
        """
        import json

        import requests

        start = time.monotonic()
        try:
            base = self._base_url or "https://generativelanguage.googleapis.com"
            url = f"{base.rstrip('/')}/v1beta/models/{self._model}:generateContent"
            body = {
                "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
                "generationConfig": {
                    "maxOutputTokens": 8,
                    "temperature": 0.0,
                },
            }
            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": self._api_key,
            }
            resp = requests.post(
                url, json=body, headers=headers,
                timeout=(min(10.0, self._timeout), self._timeout),
            )
            latency = int((time.monotonic() - start) * 1000)
            if resp.status_code != 200:
                return {
                    "ok": False,
                    "latency_ms": latency,
                    "error": f"HTTP {resp.status_code}: {resp.text[:300]}",
                }
            data = resp.json()
            text = ""
            try:
                text = (
                    data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                )
            except Exception:
                text = ""
            return {
                "ok": True,
                "latency_ms": latency,
                "sample": (text or "")[:100],
                "model": self._model,
            }
        except Exception as exc:  # noqa: BLE001
            latency = int((time.monotonic() - start) * 1000)
            logger.warning(
                "gemini test_ping failed model=%s base=%s: %s",
                self._model, self._base_url, exc,
            )
            return {
                "ok": False,
                "latency_ms": latency,
                "error": str(exc)[:500],
            }
