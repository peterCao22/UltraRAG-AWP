"""Phase 11.1.D 远程 Reranker 适配器（局域网 HTTP /rerank 接口）。

用户在局域网 vLLM 部署了 Qwen3-Reranker-8B：
    base_url: http://192.168.8.44:8022
    接口形态：POST /rerank
        请求体: {"query": str, "documents": [str]}
        响应体: {"results": [{"index": int, "document": {...}, "relevance_score": float}, ...]}
                 按 relevance_score 降序

设计原则（与 LocalReranker 接口兼容）：
    - 实现 rerank() 和 rerank_items() 两个方法，签名与 LocalReranker 一致
    - 失败重试：本地服务偶发 5xx 时退避重试；最终失败让上层 rerank_skip_reason 接管降级
    - 不缓存模型（远程服务自己管），所以 device 概念变成 "remote"

env / yaml 配置：
    ULTRARAG_RERANK_BACKEND_URL  默认 http://192.168.8.44:8022
    ULTRARAG_RERANK_TIMEOUT_SEC  默认 30
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests

_logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://192.168.8.44:8022"
DEFAULT_TIMEOUT = 30
_MAX_RETRIES = 3
_BACKOFF_SEC = 1.5


class RemoteReranker:
    """HTTP 远程 Reranker（与 LocalReranker 接口兼容）。

    特征：
        - 不加载模型（远程服务自管）
        - 单 query × N documents per request；服务端自管 batch_size
        - device 始终为 "remote"
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout_sec: Optional[int] = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get("ULTRARAG_RERANK_BACKEND_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self.timeout_sec = int(
            timeout_sec
            or os.environ.get("ULTRARAG_RERANK_TIMEOUT_SEC", DEFAULT_TIMEOUT)
        )
        # 与 LocalReranker 兼容字段
        self.device = "remote"
        self.device_name = self.base_url

    def rerank(
        self,
        query: str,
        documents: List[str],
        top_k: int = 5,
        min_score: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """对候选文档重排序（与 LocalReranker.rerank 同签名）。

        Returns:
            list[{"document": str, "score": float, "rank": int}] 按 score 降序
        """
        if query is None or not str(query).strip():
            raise ValueError("query 不能为空")
        # 清洗输入
        clean_docs: List[str] = []
        for d in documents:
            if d is None:
                continue
            s = str(d).strip()
            if s:
                clean_docs.append(s)
        if not clean_docs:
            return []

        payload = {"query": query, "documents": clean_docs}
        data = self._post_with_retries(payload)
        # 解析响应：results 按 score 降序
        raw_results = data.get("results") or []
        out: List[Dict[str, Any]] = []
        for it in raw_results:
            idx = it.get("index")
            score = float(it.get("relevance_score", 0.0))
            if min_score is not None and score < min_score:
                continue
            if idx is None or idx < 0 or idx >= len(clean_docs):
                continue
            out.append({"document": clean_docs[idx], "score": score})

        if top_k and top_k > 0:
            out = out[: int(top_k)]
        for i, item in enumerate(out, start=1):
            item["rank"] = i
        return out

    def rerank_items(
        self,
        query: str,
        items: List[Dict[str, Any]],
        content_key: str = "content",
        top_k: int = 5,
        min_score: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """对带 metadata 的文档对象重排（与 LocalReranker.rerank_items 同签名）。

        保留原始字段，追加 score 和 rank。
        """
        if query is None or not str(query).strip():
            raise ValueError("query 不能为空")
        if not items:
            return []

        valid_items: List[Dict[str, Any]] = []
        documents: List[str] = []
        for item in items:
            if item is None:
                continue
            content = item.get(content_key)
            if content is None:
                continue
            content_text = str(content).strip()
            if not content_text:
                continue
            valid_items.append(item)
            documents.append(content_text)

        if not documents:
            return []

        payload = {"query": query, "documents": documents}
        data = self._post_with_retries(payload)
        raw_results = data.get("results") or []

        out: List[Dict[str, Any]] = []
        for it in raw_results:
            idx = it.get("index")
            score = float(it.get("relevance_score", 0.0))
            if min_score is not None and score < min_score:
                continue
            if idx is None or idx < 0 or idx >= len(valid_items):
                continue
            merged = dict(valid_items[idx])
            merged["score"] = score
            out.append(merged)

        if top_k and top_k > 0:
            out = out[: int(top_k)]
        for i, item in enumerate(out, start=1):
            item["rank"] = i
        return out

    def _post_with_retries(self, payload: dict) -> dict:
        url = f"{self.base_url}/rerank"
        headers = {"Content-Type": "application/json"}
        last_exc: BaseException | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    url, json=payload, headers=headers, timeout=self.timeout_sec
                )
            except requests.exceptions.RequestException as e:
                last_exc = e
                _logger.warning(
                    "remote_reranker network error attempt=%d/%d err=%s",
                    attempt, _MAX_RETRIES, e,
                )
                if attempt == _MAX_RETRIES:
                    raise RuntimeError(
                        f"remote_reranker network failed after {_MAX_RETRIES} attempts: {e}"
                    ) from e
                time.sleep(_BACKOFF_SEC * attempt)
                continue

            if resp.status_code >= 500:
                last_exc = RuntimeError(f"{resp.status_code} {resp.text[:200]}")
                _logger.warning(
                    "remote_reranker 5xx attempt=%d/%d status=%d",
                    attempt, _MAX_RETRIES, resp.status_code,
                )
                if attempt == _MAX_RETRIES:
                    raise RuntimeError(
                        f"remote_reranker failed after retries: {last_exc}"
                    ) from last_exc
                time.sleep(_BACKOFF_SEC * attempt)
                continue

            if not resp.ok:
                raise RuntimeError(
                    f"remote_reranker {resp.status_code}: {resp.text[:200]}"
                )

            try:
                return resp.json()
            except ValueError as e:
                raise RuntimeError(
                    f"remote_reranker invalid JSON: {resp.text[:200]}"
                ) from e
        raise RuntimeError("remote_reranker: unreachable retry path")


_default_remote_reranker: Optional[RemoteReranker] = None


def get_remote_reranker(
    base_url: Optional[str] = None, timeout_sec: Optional[int] = None
) -> RemoteReranker:
    """单例工厂（与 get_default_reranker 配对的远程版本）。"""
    global _default_remote_reranker
    if _default_remote_reranker is None:
        _default_remote_reranker = RemoteReranker(
            base_url=base_url, timeout_sec=timeout_sec
        )
    return _default_remote_reranker
