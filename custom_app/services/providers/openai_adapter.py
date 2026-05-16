"""Phase 7: OpenAI / OpenAI 兼容（vLLM）适配器（最小可用版本）。

MVP 范围：
    - test_ping()：发短 prompt 验证 api_key / base_url 可达
    - chat_completion()：非流式 + 不带 tool calling 的简单一次性请求；
      用于"测试连接"和未来轻量场景
    - 复杂的 streaming + tool calling 留作下次 PR；当 Runner 实际选到 openai
      adapter 时由上层降级或抛 NotSupported。

依赖 `openai` SDK（pip install openai）。lazy import 让缺包时不影响其它路径。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class OpenAIDependencyMissing(RuntimeError):
    """openai SDK 未安装。"""


class OpenAIAdapter:
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

    def _client(self):
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as e:
            raise OpenAIDependencyMissing(
                "openai SDK not installed; run `pip install openai`"
            ) from e
        kwargs: dict[str, Any] = {"api_key": self._api_key, "timeout": self._timeout}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        return OpenAI(**kwargs)

    def chat_completion(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 64,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """非流式 chat completion；返回 {text, finish_reason, model}。"""
        client = self._client()
        resp = client.chat.completions.create(
            model=self._model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        choice = resp.choices[0]
        return {
            "text": choice.message.content or "",
            "finish_reason": choice.finish_reason,
            "model": resp.model,
        }

    def test_ping(self) -> dict[str, Any]:
        """发一条 "ping" 短 prompt，返回 {ok, latency_ms, error?, sample?}。

        失败不抛——把异常打包成 dict，便于 admin UI 直接显示。
        """
        start = time.monotonic()
        try:
            out = self.chat_completion(
                [{"role": "user", "content": "ping"}],
                max_tokens=8, temperature=0.0,
            )
            latency = int((time.monotonic() - start) * 1000)
            return {
                "ok": True,
                "latency_ms": latency,
                "sample": (out.get("text") or "")[:100],
                "model": out.get("model"),
            }
        except Exception as exc:  # noqa: BLE001 - 我们要把所有失败都给前端
            latency = int((time.monotonic() - start) * 1000)
            logger.warning(
                "openai test_ping failed model=%s base=%s: %s",
                self._model, self._base_url, exc,
            )
            return {
                "ok": False,
                "latency_ms": latency,
                "error": str(exc)[:500],
            }
