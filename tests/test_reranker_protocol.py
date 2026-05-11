"""Phase 4.0 — Reranker Protocol + LocalReranker 接入测试。

验证：
    - Reranker Protocol 可被运行时检查
    - LocalReranker 类实现了 Protocol（无需实际加载模型，只检查方法签名）
    - reset_default_reranker 能清空单例

注意：不在 CI 加载真实 bge-reranker-v2-m3 模型（GB 级）；
真实模型行为由 0.6 烟囱测试人工验证。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from custom_app.services.reranker.base import Reranker


class FakeReranker:
    """符合 Reranker Protocol 的最小实现，用于 RagRunner 单元测试注入。"""

    def __init__(self, scores: dict[str, float] | None = None) -> None:
        self._scores = scores or {}

    def rerank_items(
        self,
        query: str,
        items: list[dict[str, Any]],
        content_key: str = "content",
        top_k: int = 5,
        min_score: float | None = None,
    ) -> list[dict[str, Any]]:
        scored = []
        for it in items:
            content = it.get(content_key, "")
            score = self._scores.get(content, 0.5)
            new = dict(it)
            new["score"] = score
            scored.append(new)
        scored.sort(key=lambda x: x["score"], reverse=True)
        if top_k > 0:
            scored = scored[:top_k]
        for rank, item in enumerate(scored, start=1):
            item["rank"] = rank
        return scored


def test_fake_reranker_satisfies_protocol():
    r = FakeReranker()
    assert isinstance(r, Reranker)


def test_fake_reranker_basic_ordering():
    r = FakeReranker(scores={"doc-a": 0.9, "doc-b": 0.5, "doc-c": 0.1})
    items = [
        {"id": 1, "content": "doc-c"},
        {"id": 2, "content": "doc-a"},
        {"id": 3, "content": "doc-b"},
    ]
    out = r.rerank_items(query="q", items=items, top_k=3)
    assert [x["id"] for x in out] == [2, 3, 1]
    assert [x["rank"] for x in out] == [1, 2, 3]


def test_local_reranker_class_is_a_reranker():
    """LocalReranker 类（不实例化模型）应符合 Protocol 的方法签名。"""
    pytest.importorskip("torch", reason="torch not installed")
    from custom_app.utils.local_reranker import LocalReranker
    assert hasattr(LocalReranker, "rerank_items")
    assert callable(getattr(LocalReranker, "rerank_items"))


def test_reset_default_reranker_clears_singleton():
    """reset_default_reranker 应使下次 get_default_reranker 重新加载。"""
    pytest.importorskip("torch", reason="torch not installed")
    import custom_app.utils.local_reranker as lr_mod

    sentinel = object()
    lr_mod._default_reranker = sentinel  # type: ignore[assignment]
    assert lr_mod._default_reranker is sentinel

    lr_mod.reset_default_reranker()
    assert lr_mod._default_reranker is None
    assert lr_mod._default_reranker_config is None


def test_get_default_reranker_accepts_config_args():
    """get_default_reranker 应接受 model_path/batch_size/device 参数并透传。"""
    pytest.importorskip("torch", reason="torch not installed")
    import custom_app.utils.local_reranker as lr_mod

    lr_mod.reset_default_reranker()
    with patch.object(lr_mod, "LocalReranker") as mock_cls:
        mock_cls.return_value = object()
        result = lr_mod.get_default_reranker(
            model_path=r"X:\fake\path",
            batch_size=2,
            device="cpu",
        )
        assert result is mock_cls.return_value
        mock_cls.assert_called_once()
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["model_path"] == r"X:\fake\path"
        assert kwargs["batch_size"] == 2
        assert kwargs["device"] == "cpu"

    lr_mod.reset_default_reranker()
