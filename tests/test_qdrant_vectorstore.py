"""Phase 5.1.1 — QdrantVectorStore 单元测试。

通过 mock qdrant-client 验证 VectorStore Protocol 行为；
真实 Qdrant 服务的烟囱测试用 @pytest.mark.requires_qdrant 标记，CI 默认 skip。
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


pytest.importorskip("qdrant_client", reason="qdrant-client not installed in this env")


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------


def test_config_from_env(monkeypatch):
    from custom_app.services.vectorstore.qdrant_store import QdrantConfig

    monkeypatch.setenv("ULTRARAG_QDRANT_URL", "http://test:6333")
    monkeypatch.setenv("ULTRARAG_QDRANT_API_KEY", "secret")
    monkeypatch.setenv("ULTRARAG_QDRANT_COLLECTION_PREFIX", "myapp")
    monkeypatch.setenv("ULTRARAG_QDRANT_TIMEOUT_SEC", "60")

    cfg = QdrantConfig.from_env()
    assert cfg.url == "http://test:6333"
    assert cfg.api_key == "secret"
    assert cfg.collection_prefix == "myapp"
    assert cfg.timeout_sec == 60


def test_config_missing_url_raises(monkeypatch):
    from custom_app.services.vectorstore.qdrant_store import QdrantConfig

    monkeypatch.delenv("ULTRARAG_QDRANT_URL", raising=False)
    with pytest.raises(ValueError, match="ULTRARAG_QDRANT_URL"):
        QdrantConfig.from_env()


def test_config_empty_api_key_is_none(monkeypatch):
    from custom_app.services.vectorstore.qdrant_store import QdrantConfig

    monkeypatch.setenv("ULTRARAG_QDRANT_URL", "http://x:6333")
    monkeypatch.setenv("ULTRARAG_QDRANT_API_KEY", "")
    cfg = QdrantConfig.from_env()
    assert cfg.api_key is None


# ---------------------------------------------------------------------------
# Mock client 验证 Protocol 实现
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_config():
    from custom_app.services.vectorstore.qdrant_store import QdrantConfig

    return QdrantConfig(url="http://mock", collection_prefix="test", timeout_sec=5)


@pytest.fixture
def store_with_mock(fake_config):
    """构造 QdrantVectorStore 实例 + mock 的 QdrantClient。"""
    from custom_app.services.vectorstore.qdrant_store import QdrantVectorStore

    with patch(
        "custom_app.services.vectorstore.qdrant_store.QdrantVectorStore._build_client"
    ) as build:
        mock_client = MagicMock()
        build.return_value = mock_client
        store = QdrantVectorStore(kb_id="demo", config=fake_config, embed_dim=4)
        store._client = mock_client  # 防御性确认
        yield store, mock_client


def test_collection_name_includes_prefix_and_kb(store_with_mock):
    store, _ = store_with_mock
    assert store.collection_name == "test__demo"


def test_ensure_collection_creates_when_absent(store_with_mock):
    store, client = store_with_mock
    client.collection_exists.return_value = False
    store.ensure_collection()
    client.create_collection.assert_called_once()
    # 2 个 payload 索引：kb_id + doc
    assert client.create_payload_index.call_count == 2


def test_ensure_collection_skips_when_present(store_with_mock):
    store, client = store_with_mock
    client.collection_exists.return_value = True
    store.ensure_collection(recreate=False)
    client.create_collection.assert_not_called()


def test_ensure_collection_recreate(store_with_mock):
    store, client = store_with_mock
    client.collection_exists.return_value = True
    store.ensure_collection(recreate=True)
    client.delete_collection.assert_called_once_with("test__demo")
    client.create_collection.assert_called_once()


def test_upsert_validates_length_mismatch(store_with_mock):
    store, _ = store_with_mock
    vecs = np.zeros((2, 4), dtype="float32")
    with pytest.raises(ValueError, match="chunk_ids length"):
        store.upsert(["a"], vecs)  # 1 id vs 2 vectors

    with pytest.raises(ValueError, match="payloads length"):
        store.upsert(["a", "b"], vecs, payloads=[{"doc": "x"}])  # 1 payload vs 2 ids


def test_upsert_injects_kb_id_and_chunk_id(store_with_mock):
    store, client = store_with_mock
    vecs = np.zeros((2, 4), dtype="float32")
    store.upsert(["chunk_a", "chunk_b"], vecs, payloads=[{"doc": "d1"}, {"doc": "d2"}])

    # 应至少调用一次 upsert
    client.upsert.assert_called()
    # 检查传入的 points
    call_args = client.upsert.call_args
    points = call_args.kwargs["points"]
    assert len(points) == 2
    assert points[0].payload["kb_id"] == "demo"
    assert points[0].payload["chunk_id"] == "chunk_a"
    assert points[1].payload["chunk_id"] == "chunk_b"


def test_search_returns_hits_with_chunk_id_from_payload(store_with_mock):
    store, client = store_with_mock

    mock_point = MagicMock()
    mock_point.id = 1234567  # hash 后的 int
    mock_point.score = 0.95
    mock_point.payload = {"chunk_id": "doc_step_1"}

    mock_response = MagicMock()
    mock_response.points = [mock_point]
    client.query_points.return_value = mock_response

    hits = store.search(np.zeros(4, dtype="float32"), top_k=5)
    assert len(hits) == 1
    assert hits[0].chunk_id == "doc_step_1"  # 来自 payload，不是 point.id
    assert hits[0].score == pytest.approx(0.95)


def test_search_top_k_zero_returns_empty(store_with_mock):
    store, _ = store_with_mock
    assert store.search(np.zeros(4, dtype="float32"), top_k=0) == []


def test_search_builds_filter_for_single_value(store_with_mock):
    store, client = store_with_mock
    mock_response = MagicMock()
    mock_response.points = []
    client.query_points.return_value = mock_response

    store.search(np.zeros(4, dtype="float32"), top_k=5, filter={"doc": "doc1"})
    # 查询参数里应带 query_filter
    call = client.query_points.call_args
    assert call.kwargs.get("query_filter") is not None


def test_search_builds_filter_for_multi_value(store_with_mock):
    store, client = store_with_mock
    mock_response = MagicMock()
    mock_response.points = []
    client.query_points.return_value = mock_response

    store.search(np.zeros(4, dtype="float32"), top_k=5, filter={"doc": ["a", "b"]})
    call = client.query_points.call_args
    assert call.kwargs.get("query_filter") is not None


def test_delete_no_op_on_empty(store_with_mock):
    store, client = store_with_mock
    store.delete([])
    client.delete.assert_not_called()


def test_delete_calls_qdrant_delete(store_with_mock):
    store, client = store_with_mock
    store.delete(["chunk_a", "chunk_b"])
    client.delete.assert_called_once()


def test_size_returns_zero_when_collection_missing(store_with_mock):
    store, client = store_with_mock
    client.collection_exists.return_value = False
    assert store.size() == 0


def test_size_returns_points_count(store_with_mock):
    store, client = store_with_mock
    client.collection_exists.return_value = True
    info = MagicMock()
    info.points_count = 42
    client.get_collection.return_value = info
    assert store.size() == 42


def test_protocol_compliance(store_with_mock):
    """QdrantVectorStore 必须符合 VectorStore Protocol。"""
    from custom_app.services.vectorstore.base import VectorStore

    store, _ = store_with_mock
    assert isinstance(store, VectorStore)


# ---------------------------------------------------------------------------
# point_id 映射
# ---------------------------------------------------------------------------


def test_string_id_to_point_id_is_deterministic():
    from custom_app.services.vectorstore.qdrant_store import _string_id_to_point_id

    a = _string_id_to_point_id("chunk_a")
    b = _string_id_to_point_id("chunk_a")
    assert a == b


def test_string_id_to_point_id_differs_for_different_inputs():
    from custom_app.services.vectorstore.qdrant_store import _string_id_to_point_id

    assert _string_id_to_point_id("a") != _string_id_to_point_id("b")


def test_string_id_to_point_id_is_unsigned():
    from custom_app.services.vectorstore.qdrant_store import _string_id_to_point_id

    for s in ["a", "very_long_chunk_id_with_unicode_中文", "step_999"]:
        pid = _string_id_to_point_id(s)
        assert isinstance(pid, int)
        assert 0 <= pid < 2**64


# ---------------------------------------------------------------------------
# 真实 Qdrant 集成测试
# ---------------------------------------------------------------------------


@pytest.mark.requires_qdrant
def test_integration_real_qdrant_roundtrip(monkeypatch):
    """需要真实 Qdrant 服务；运行：pytest -m requires_qdrant"""
    from dotenv import load_dotenv

    load_dotenv()
    if not os.environ.get("ULTRARAG_QDRANT_URL"):
        pytest.skip("ULTRARAG_QDRANT_URL not set")

    from custom_app.services.vectorstore.qdrant_store import QdrantVectorStore

    store = QdrantVectorStore(kb_id="phase5_test_integration", embed_dim=4)
    try:
        store.ensure_collection(recreate=True)
        vecs = np.array(
            [[1, 0, 0, 0], [0.9, 0.1, 0, 0], [0, 1, 0, 0]],
            dtype="float32",
        )
        store.upsert(
            ["a", "b", "c"],
            vecs,
            payloads=[
                {"doc": "d1"},
                {"doc": "d1"},
                {"doc": "d2"},
            ],
        )
        assert store.size() == 3

        hits = store.search(np.array([1, 0, 0, 0], dtype="float32"), top_k=3)
        assert [h.chunk_id for h in hits] == ["a", "b", "c"]
        assert hits[0].score > hits[1].score > hits[2].score

        # filter
        hits = store.search(
            np.array([1, 0, 0, 0], dtype="float32"),
            top_k=3,
            filter={"doc": "d2"},
        )
        assert [h.chunk_id for h in hits] == ["c"]
    finally:
        store.delete_collection()
