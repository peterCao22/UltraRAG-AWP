"""Phase 11.1.C —— 本地 Embedding 适配器 + backend 路由测试。

策略：mock requests.post；不真打 192.168.8.44 服务。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import requests

from custom_app.services import local_embedder
from custom_app.services.google_embedder import _resolved_backend


def _mock_embeddings_response(vectors: list[list[float]]) -> MagicMock:
    """构造 OpenAI 兼容 /v1/embeddings 响应。"""
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.json.return_value = {
        "data": [
            {"object": "embedding", "index": i, "embedding": v}
            for i, v in enumerate(vectors)
        ],
        "model": "qwen3-embedding-8b",
    }
    return resp


class TestLocalEmbedderBasic:
    def test_empty_input_returns_empty_array(self) -> None:
        out = local_embedder.embed_texts([])
        assert out.shape == (0, 4096)

    def test_single_text_returns_normalized_vector(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ULTRARAG_EMBED_DIM", raising=False)
        fake_vec = [3.0, 4.0] + [0.0] * 4094  # ||v||=5
        with patch.object(
            requests, "post", return_value=_mock_embeddings_response([fake_vec])
        ) as mock_post:
            out = local_embedder.embed_texts(["hello"])
        assert out.shape == (1, 4096)
        # L2 归一化后 [0.6, 0.8, 0, ...]
        assert out[0][0] == pytest.approx(0.6, abs=1e-5)
        assert out[0][1] == pytest.approx(0.8, abs=1e-5)
        # 调用 url + payload 正确
        call_args = mock_post.call_args
        assert call_args.kwargs["json"] == {
            "input": ["hello"],
            "model": "qwen3-embedding-8b",
        }

    def test_embed_query_returns_1d_vector(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ULTRARAG_EMBED_DIM", raising=False)
        fake_vec = [1.0] + [0.0] * 4095
        with patch.object(
            requests, "post", return_value=_mock_embeddings_response([fake_vec])
        ):
            out = local_embedder.embed_query("test")
        assert out.shape == (4096,)

    def test_batch_chunking(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """超过 batch_size 自动分批。"""
        monkeypatch.setenv("ULTRARAG_EMBED_BATCH_SIZE", "2")
        monkeypatch.delenv("ULTRARAG_EMBED_DIM", raising=False)
        # 5 个 texts，batch_size=2 → 3 次调用
        fake_vec = [1.0] + [0.0] * 4095
        post_count = [0]

        def _fake_post(url, json=None, headers=None, timeout=None):
            post_count[0] += 1
            n = len(json["input"])
            return _mock_embeddings_response([fake_vec] * n)

        with patch.object(requests, "post", side_effect=_fake_post):
            out = local_embedder.embed_texts(["a", "b", "c", "d", "e"])
        assert out.shape == (5, 4096)
        assert post_count[0] == 3  # 2 + 2 + 1


class TestLocalEmbedderRetry:
    def test_500_triggers_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 第 1 次 500，第 2 次成功
        resp500 = MagicMock()
        resp500.status_code = 500
        resp500.ok = False
        resp500.text = "internal error"

        fake_vec = [1.0] + [0.0] * 4095
        resp200 = _mock_embeddings_response([fake_vec])

        with patch.object(requests, "post", side_effect=[resp500, resp200]):
            with patch.object(local_embedder.time, "sleep"):
                out = local_embedder.embed_texts(["q"])
        assert out.shape == (1, 4096)

    def test_persistent_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp500 = MagicMock()
        resp500.status_code = 500
        resp500.ok = False
        resp500.text = "down"
        with patch.object(requests, "post", return_value=resp500):
            with patch.object(local_embedder.time, "sleep"):
                with pytest.raises(RuntimeError, match="failed after retries"):
                    local_embedder.embed_texts(["q"])

    def test_network_error_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_vec = [1.0] + [0.0] * 4095
        success = _mock_embeddings_response([fake_vec])

        with patch.object(
            requests, "post",
            side_effect=[requests.exceptions.ConnectionError("eof"), success],
        ):
            with patch.object(local_embedder.time, "sleep"):
                out = local_embedder.embed_texts(["q"])
        assert out.shape == (1, 4096)

    def test_4xx_raises_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """400/401/403 不重试，立即抛错。"""
        resp = MagicMock()
        resp.status_code = 400
        resp.ok = False
        resp.text = "bad request"
        with patch.object(requests, "post", return_value=resp):
            with pytest.raises(RuntimeError, match="400"):
                local_embedder.embed_texts(["q"])


class TestBackendRouting:
    def test_default_is_gemini(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ULTRARAG_EMBED_BACKEND", raising=False)
        assert _resolved_backend() == "gemini"

    def test_env_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ULTRARAG_EMBED_BACKEND", "local")
        assert _resolved_backend() == "local"

    def test_env_invalid_falls_back_gemini(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ULTRARAG_EMBED_BACKEND", "bogus")
        assert _resolved_backend() == "gemini"

    def test_local_route_calls_local_embedder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """env=local 时 google_embedder.embed_texts 调本地实现。"""
        monkeypatch.setenv("ULTRARAG_EMBED_BACKEND", "local")
        from custom_app.services import google_embedder

        with patch.object(local_embedder, "embed_texts") as mock_local:
            mock_local.return_value = np.zeros((1, 4096), dtype=np.float32)
            google_embedder.embed_texts(["q"])
        mock_local.assert_called_once()
