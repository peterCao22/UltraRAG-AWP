"""Phase 11.1.C 本地 Embedding 适配器（OpenAI 兼容 /v1/embeddings）。

用户在局域网 vLLM 部署了 Qwen3-Embedding-8B：
    base_url: http://192.168.8.44:8021/v1
    model:    qwen3-embedding-8b
    dim:      4096

设计原则：
    - 接口与 google_embedder.embed_texts / embed_query 一致（np.ndarray 输出 + L2 归一化）
    - 与 Gemini API 一致输出 float32 + L2 归一化（方便上下游沿用）
    - 失败重试：本地服务偶发 500/502 时退避重试；最终失败抛 RuntimeError
    - 不强制 task_type（Gemini 概念）；Qwen3 单 endpoint 通吃

env / yaml 配置：
    ULTRARAG_EMBED_BACKEND_URL  默认 http://192.168.8.44:8021/v1
    ULTRARAG_EMBED_MODEL        默认 qwen3-embedding-8b
    ULTRARAG_EMBED_DIM          默认 4096（必须与服务真实返回维度一致）
    ULTRARAG_EMBED_BATCH_SIZE   默认 16
    ULTRARAG_EMBED_TIMEOUT_SEC  默认 60
"""
from __future__ import annotations

import logging
import os
import time
from typing import List, Sequence

import numpy as np
import requests

_logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://192.168.8.44:8021/v1"
DEFAULT_MODEL = "qwen3-embedding-8b"
DEFAULT_DIM = 4096
DEFAULT_BATCH_SIZE = 16
DEFAULT_TIMEOUT = 60
_MAX_RETRIES = 3
_BACKOFF_SEC = 1.5


def _resolved_config() -> dict:
    return {
        "base_url": os.environ.get(
            "ULTRARAG_EMBED_BACKEND_URL", DEFAULT_BASE_URL
        ).rstrip("/"),
        "model": os.environ.get("ULTRARAG_EMBED_MODEL", DEFAULT_MODEL),
        "dim": int(os.environ.get("ULTRARAG_EMBED_DIM", str(DEFAULT_DIM))),
        "batch_size": int(
            os.environ.get("ULTRARAG_EMBED_BATCH_SIZE", str(DEFAULT_BATCH_SIZE))
        ),
        "timeout_sec": int(
            os.environ.get("ULTRARAG_EMBED_TIMEOUT_SEC", str(DEFAULT_TIMEOUT))
        ),
    }


def embed_texts(
    texts: Sequence[str], task_type: str = "RETRIEVAL_DOCUMENT"
) -> np.ndarray:
    """批量将文本列表转换为向量矩阵（与 google_embedder.embed_texts 同签名）。

    Args:
        texts: 文本列表
        task_type: 兼容签名（Gemini 概念），本实现忽略——Qwen3 单 endpoint

    Returns:
        shape (N, DIM) 的 float32 ndarray，已 L2 归一化
    """
    if not texts:
        return np.zeros((0, _resolved_config()["dim"]), dtype=np.float32)

    cfg = _resolved_config()
    url = f"{cfg['base_url']}/embeddings"
    headers = {"Content-Type": "application/json"}

    all_vecs: List[np.ndarray] = []
    total = len(texts)
    batch = cfg["batch_size"]
    for start in range(0, total, batch):
        chunk = list(texts[start : start + batch])
        payload = {"input": chunk, "model": cfg["model"]}
        data = _post_with_retries(url, payload, headers, cfg["timeout_sec"])
        # OpenAI 兼容：data["data"] = [{"object":"embedding","index":i,"embedding":[...]}]
        items = data.get("data") or []
        if len(items) != len(chunk):
            raise RuntimeError(
                f"local_embedder: expected {len(chunk)} embeddings, got {len(items)}"
            )
        for it in sorted(items, key=lambda x: x.get("index", 0)):
            vec = np.asarray(it["embedding"], dtype=np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            all_vecs.append(vec)
        done = min(start + batch, total)
        _logger.info("local embed progress: %d/%d", done, total)

    arr = np.array(all_vecs, dtype=np.float32)
    expected_dim = cfg["dim"]
    if arr.size and arr.shape[1] != expected_dim:
        _logger.warning(
            "local_embedder dim mismatch: got %d, expected %d (ULTRARAG_EMBED_DIM)",
            arr.shape[1],
            expected_dim,
        )
    return arr


def embed_query(query: str) -> np.ndarray:
    """单条查询向量。"""
    return embed_texts([query])[0]


def _post_with_retries(
    url: str, payload: dict, headers: dict, timeout: int
) -> dict:
    """指数退避重试；最终失败抛 RuntimeError。"""
    last_exc: BaseException | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as e:
            last_exc = e
            _logger.warning(
                "local_embedder network error attempt=%d/%d err=%s",
                attempt, _MAX_RETRIES, e,
            )
            if attempt == _MAX_RETRIES:
                raise RuntimeError(
                    f"local_embedder network failed after {_MAX_RETRIES} attempts: {e}"
                ) from e
            time.sleep(_BACKOFF_SEC * attempt)
            continue

        if resp.status_code >= 500:
            last_exc = RuntimeError(f"{resp.status_code} {resp.text[:200]}")
            _logger.warning(
                "local_embedder 5xx attempt=%d/%d status=%d",
                attempt, _MAX_RETRIES, resp.status_code,
            )
            if attempt == _MAX_RETRIES:
                raise RuntimeError(
                    f"local_embedder failed after retries: {last_exc}"
                ) from last_exc
            time.sleep(_BACKOFF_SEC * attempt)
            continue

        if not resp.ok:
            raise RuntimeError(
                f"local_embedder {resp.status_code}: {resp.text[:200]}"
            )

        try:
            return resp.json()
        except ValueError as e:
            raise RuntimeError(
                f"local_embedder invalid JSON: {resp.text[:200]}"
            ) from e
    # unreachable
    raise RuntimeError("local_embedder: unreachable retry path")
