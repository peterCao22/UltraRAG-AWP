"""Phase 11.1.D —— 远程 Reranker 适配器测试。

策略：mock requests.post，不真打 192.168.8.44。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from custom_app.utils import remote_reranker
from custom_app.utils.remote_reranker import RemoteReranker


def _mock_rerank_response(results: list[dict]) -> MagicMock:
    """构造 /rerank 响应。

    results 格式：[{"index": int, "relevance_score": float, "document": str | None}]
    """
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.json.return_value = {
        "id": "score-mocked",
        "model": "qwen3-reranker-8b",
        "usage": {"prompt_tokens": 30, "total_tokens": 30},
        "results": [
            {
                "index": r["index"],
                "document": {"text": r.get("document", "x"), "multi_modal": None},
                "relevance_score": r["relevance_score"],
            }
            for r in results
        ],
    }
    return resp


class TestRerank:
    def test_basic_returns_sorted_with_rank(self) -> None:
        rr = RemoteReranker(base_url="http://example/8022")
        docs = ["A", "B", "C"]
        # 服务端返回按 score 降序的 idx
        with patch.object(
            requests, "post",
            return_value=_mock_rerank_response([
                {"index": 2, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.7},
                {"index": 1, "relevance_score": 0.5},
            ]),
        ) as mock_post:
            out = rr.rerank("q", docs, top_k=3)

        assert [o["document"] for o in out] == ["C", "A", "B"]
        assert [o["rank"] for o in out] == [1, 2, 3]
        assert out[0]["score"] == 0.9
        # url 与 payload
        call = mock_post.call_args
        assert "http://example/8022/rerank" in call.args[0]
        assert call.kwargs["json"] == {"query": "q", "documents": docs}

    def test_top_k_truncates(self) -> None:
        rr = RemoteReranker(base_url="http://e")
        docs = ["A", "B", "C"]
        with patch.object(
            requests, "post",
            return_value=_mock_rerank_response([
                {"index": 0, "relevance_score": 0.9},
                {"index": 1, "relevance_score": 0.8},
                {"index": 2, "relevance_score": 0.5},
            ]),
        ):
            out = rr.rerank("q", docs, top_k=2)
        assert len(out) == 2

    def test_min_score_filter(self) -> None:
        rr = RemoteReranker(base_url="http://e")
        docs = ["A", "B", "C"]
        with patch.object(
            requests, "post",
            return_value=_mock_rerank_response([
                {"index": 0, "relevance_score": 0.9},
                {"index": 1, "relevance_score": 0.5},
                {"index": 2, "relevance_score": 0.3},
            ]),
        ):
            out = rr.rerank("q", docs, top_k=5, min_score=0.4)
        assert [o["document"] for o in out] == ["A", "B"]

    def test_empty_query_raises(self) -> None:
        rr = RemoteReranker(base_url="http://e")
        with pytest.raises(ValueError, match="query"):
            rr.rerank("  ", ["A"])

    def test_empty_documents(self) -> None:
        rr = RemoteReranker(base_url="http://e")
        assert rr.rerank("q", []) == []

    def test_strips_blanks(self) -> None:
        rr = RemoteReranker(base_url="http://e")
        with patch.object(
            requests, "post",
            return_value=_mock_rerank_response([
                {"index": 0, "relevance_score": 0.9},
            ]),
        ) as mock_post:
            rr.rerank("q", ["", "  ", "valid"])
        # 服务端只收到 "valid"（清洗了空字符串）
        assert mock_post.call_args.kwargs["json"]["documents"] == ["valid"]


class TestRerankItems:
    def test_preserves_original_fields(self) -> None:
        rr = RemoteReranker(base_url="http://e")
        items = [
            {"row_idx": 10, "content": "doc A"},
            {"row_idx": 20, "content": "doc B"},
            {"row_idx": 30, "content": "doc C"},
        ]
        with patch.object(
            requests, "post",
            return_value=_mock_rerank_response([
                {"index": 1, "relevance_score": 0.95},
                {"index": 2, "relevance_score": 0.7},
                {"index": 0, "relevance_score": 0.5},
            ]),
        ):
            out = rr.rerank_items("q", items, content_key="content", top_k=3)
        # row_idx 在重排后保留
        assert [o["row_idx"] for o in out] == [20, 30, 10]
        assert out[0]["score"] == 0.95
        assert out[0]["rank"] == 1

    def test_skips_missing_content_key(self) -> None:
        rr = RemoteReranker(base_url="http://e")
        items = [
            {"row_idx": 1},  # 无 content 字段
            {"row_idx": 2, "content": "real"},
            {"row_idx": 3, "content": ""},  # 空字符串
        ]
        with patch.object(
            requests, "post",
            return_value=_mock_rerank_response([
                {"index": 0, "relevance_score": 0.9},
            ]),
        ) as mock_post:
            out = rr.rerank_items("q", items, content_key="content")
        # 只 1 个有效 item
        assert mock_post.call_args.kwargs["json"]["documents"] == ["real"]
        assert len(out) == 1
        assert out[0]["row_idx"] == 2


class TestRetry:
    def test_500_then_success(self) -> None:
        rr = RemoteReranker(base_url="http://e")
        resp500 = MagicMock(status_code=500, ok=False, text="oops")
        good = _mock_rerank_response([{"index": 0, "relevance_score": 0.5}])
        with patch.object(requests, "post", side_effect=[resp500, good]):
            with patch.object(remote_reranker.time, "sleep"):
                out = rr.rerank("q", ["A"])
        assert out and out[0]["score"] == 0.5

    def test_4xx_raises_immediately(self) -> None:
        rr = RemoteReranker(base_url="http://e")
        resp = MagicMock(status_code=400, ok=False, text="bad req")
        with patch.object(requests, "post", return_value=resp):
            with pytest.raises(RuntimeError, match="400"):
                rr.rerank("q", ["A"])

    def test_network_error_retries(self) -> None:
        rr = RemoteReranker(base_url="http://e")
        good = _mock_rerank_response([{"index": 0, "relevance_score": 0.5}])
        with patch.object(
            requests, "post",
            side_effect=[requests.exceptions.ConnectionError("eof"), good],
        ):
            with patch.object(remote_reranker.time, "sleep"):
                out = rr.rerank("q", ["A"])
        assert out


class TestInterfaceCompat:
    def test_device_attr(self) -> None:
        """与 LocalReranker.device 接口兼容。"""
        rr = RemoteReranker(base_url="http://e")
        assert rr.device == "remote"

    def test_factory_singleton(self) -> None:
        """get_remote_reranker 返回同一实例。"""
        from custom_app.utils.remote_reranker import (
            get_remote_reranker, _default_remote_reranker as _,  # noqa: F401
        )
        # 重置单例
        import custom_app.utils.remote_reranker as rr_mod
        rr_mod._default_remote_reranker = None
        a = get_remote_reranker(base_url="http://e")
        b = get_remote_reranker(base_url="http://e2")  # 参数忽略，复用第一个
        assert a is b
        rr_mod._default_remote_reranker = None  # 清理
