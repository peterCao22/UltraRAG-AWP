"""Phase 8.2.2.c —— RagRunner 双路检索改造单元测试。

策略：用 stub _rows + monkeypatch BM25Store 与 RRF，避免触发 FAISS/Qdrant/Gemini。
覆盖：
    - _resolve_retrieval_mode：env > yaml > 默认 hybrid
    - _rrf_params：默认值 / yaml 覆盖
    - _load_bm25_if_enabled：mode=vector 时跳过；hybrid 时构建；构建失败降级到 None
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_app.services.rag_runner import RagRunner


def _make_runner_with_rows(rows: list[dict]) -> RagRunner:
    """构造 runner，只设最小字段供 BM25 / mode 测试使用，不调 init()。"""
    runner = RagRunner.__new__(RagRunner)
    runner.kb_id = "test_kb"
    runner._rows = rows
    runner._bm25_store = None
    runner._bm25_load_error = None
    runner._retrieval_cfg = {}
    return runner


class TestResolveRetrievalMode:
    def test_default_is_hybrid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ULTRARAG_RETRIEVAL_MODE", raising=False)
        runner = _make_runner_with_rows([])
        assert runner._resolve_retrieval_mode() == "hybrid"

    def test_env_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ULTRARAG_RETRIEVAL_MODE", "vector")
        runner = _make_runner_with_rows([])
        assert runner._resolve_retrieval_mode() == "vector"

    def test_env_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ULTRARAG_RETRIEVAL_MODE", "vector")
        runner = _make_runner_with_rows([])
        runner._retrieval_cfg = {"mode": "hybrid"}
        assert runner._resolve_retrieval_mode() == "vector"

    def test_yaml_used_when_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ULTRARAG_RETRIEVAL_MODE", raising=False)
        runner = _make_runner_with_rows([])
        runner._retrieval_cfg = {"mode": "vector"}
        assert runner._resolve_retrieval_mode() == "vector"

    def test_invalid_falls_back_to_hybrid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ULTRARAG_RETRIEVAL_MODE", "bogus")
        runner = _make_runner_with_rows([])
        assert runner._resolve_retrieval_mode() == "hybrid"


class TestRrfParams:
    def test_defaults(self) -> None:
        runner = _make_runner_with_rows([])
        vw, kw, k = runner._rrf_params()
        assert (vw, kw, k) == (0.7, 0.3, 60)

    def test_yaml_overrides(self) -> None:
        runner = _make_runner_with_rows([])
        runner._retrieval_cfg = {
            "vector_weight": 0.6,
            "keyword_weight": 0.4,
            "rrf_k": 80,
        }
        vw, kw, k = runner._rrf_params()
        assert (vw, kw, k) == (0.6, 0.4, 80)

    def test_bad_values_fall_back_to_defaults(self) -> None:
        runner = _make_runner_with_rows([])
        runner._retrieval_cfg = {"vector_weight": "not a number"}
        vw, kw, k = runner._rrf_params()
        assert (vw, kw, k) == (0.7, 0.3, 60)


class TestLoadBm25IfEnabled:
    def test_skipped_in_vector_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ULTRARAG_RETRIEVAL_MODE", "vector")
        runner = _make_runner_with_rows([{"id": "a", "contents": "x"}])
        runner._load_bm25_if_enabled()
        assert runner._bm25_store is None

    def test_built_in_hybrid_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ULTRARAG_RETRIEVAL_MODE", "hybrid")
        runner = _make_runner_with_rows(
            [
                {"id": "a", "contents": "AGV 换电池 STEP 1"},
                {"id": "b", "contents": "IFS 登录失败"},
            ]
        )
        runner._load_bm25_if_enabled()
        assert runner._bm25_store is not None
        assert runner._bm25_store.size() == 2

    def test_build_failure_degrades_to_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """共识 §五.5：BM25 加载失败降级，runner 仍可服务（纯向量）。"""
        monkeypatch.setenv("ULTRARAG_RETRIEVAL_MODE", "hybrid")
        runner = _make_runner_with_rows([{"id": "a", "contents": "x"}])
        with patch(
            "custom_app.services.retrieval.bm25.BM25Store.from_rows",
            side_effect=ValueError("simulated boot failure"),
        ):
            runner._load_bm25_if_enabled()
        assert runner._bm25_store is None
        assert runner._bm25_load_error is not None
        assert "simulated boot failure" in runner._bm25_load_error
