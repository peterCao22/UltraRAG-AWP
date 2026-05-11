"""Phase 4.0 — FaissVectorStore 单元测试。

通过 mock faiss 模块，验证 FaissVectorStore 的对外行为：
    - load 校验 index.ntotal 与 chunk_ids 长度一致
    - search 返回按行号映射的 Hit 列表
    - upsert / delete 抛 NotImplementedError（Phase 4 不支持）
    - size 透传 index.ntotal
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest


@pytest.fixture
def mock_faiss(monkeypatch):
    """注入一个最小 faiss 模块到 sys.modules。"""
    fake = types.ModuleType("faiss")
    fake.read_index = MagicMock()
    monkeypatch.setitem(sys.modules, "faiss", fake)
    return fake


@pytest.fixture
def fake_index() -> MagicMock:
    """构造一个 mock FAISS index：ntotal=3，search 返回固定行号。"""
    idx = MagicMock()
    idx.ntotal = 3
    # search 返回 (distances, indices)：相似度倒序，行号 [2, 0, 1]
    idx.search.return_value = (
        np.array([[0.95, 0.85, 0.75]], dtype="float32"),
        np.array([[2, 0, 1]], dtype="int64"),
    )
    return idx


def test_load_validates_size(mock_faiss, fake_index, tmp_path):
    from custom_app.services.vectorstore.faiss_store import FaissVectorStore

    index_path = tmp_path / "test.index"
    index_path.write_bytes(b"fake")
    mock_faiss.read_index.return_value = fake_index

    # 长度不匹配应抛 ValueError
    with pytest.raises(ValueError, match="mismatches"):
        FaissVectorStore.load(index_path, ["a", "b"])  # ntotal=3 但只给 2 个 id

    # 长度匹配 OK
    store = FaissVectorStore.load(index_path, ["a", "b", "c"])
    assert store.size() == 3


def test_load_missing_file(mock_faiss, tmp_path):
    from custom_app.services.vectorstore.faiss_store import FaissVectorStore

    with pytest.raises(FileNotFoundError):
        FaissVectorStore.load(tmp_path / "nonexistent.index", ["a"])


def test_search_returns_chunk_ids(mock_faiss, fake_index, tmp_path):
    from custom_app.services.vectorstore.faiss_store import FaissVectorStore

    index_path = tmp_path / "test.index"
    index_path.write_bytes(b"fake")
    mock_faiss.read_index.return_value = fake_index

    store = FaissVectorStore.load(index_path, ["alpha", "beta", "gamma"])
    q = np.zeros((1, 4), dtype="float32")
    hits = store.search(q, top_k=3)

    assert len(hits) == 3
    # mock 返回行号 [2, 0, 1] → 应映射到 ["gamma", "alpha", "beta"]
    assert hits[0].chunk_id == "gamma"
    assert hits[0].score == pytest.approx(0.95)
    assert hits[1].chunk_id == "alpha"
    assert hits[2].chunk_id == "beta"


def test_search_handles_1d_query(mock_faiss, fake_index, tmp_path):
    from custom_app.services.vectorstore.faiss_store import FaissVectorStore

    index_path = tmp_path / "test.index"
    index_path.write_bytes(b"fake")
    mock_faiss.read_index.return_value = fake_index

    store = FaissVectorStore.load(index_path, ["a", "b", "c"])
    q_1d = np.zeros(4, dtype="float32")  # 1D 向量
    hits = store.search(q_1d, top_k=3)
    assert len(hits) == 3


def test_search_top_k_zero_returns_empty(mock_faiss, fake_index, tmp_path):
    from custom_app.services.vectorstore.faiss_store import FaissVectorStore

    index_path = tmp_path / "test.index"
    index_path.write_bytes(b"fake")
    mock_faiss.read_index.return_value = fake_index

    store = FaissVectorStore.load(index_path, ["a", "b", "c"])
    assert store.search(np.zeros((1, 4), dtype="float32"), top_k=0) == []


def test_search_skips_negative_indices(mock_faiss, tmp_path):
    """FAISS 返回 -1 表示"该位置无命中"，应被过滤。"""
    from custom_app.services.vectorstore.faiss_store import FaissVectorStore

    idx = MagicMock()
    idx.ntotal = 3
    idx.search.return_value = (
        np.array([[0.9, 0.0, 0.0]], dtype="float32"),
        np.array([[0, -1, -1]], dtype="int64"),
    )
    mock_faiss = sys.modules["faiss"]
    mock_faiss.read_index.return_value = idx

    index_path = tmp_path / "test.index"
    index_path.write_bytes(b"fake")
    store = FaissVectorStore.load(index_path, ["a", "b", "c"])
    hits = store.search(np.zeros((1, 4), dtype="float32"), top_k=3)
    assert len(hits) == 1
    assert hits[0].chunk_id == "a"


def test_upsert_and_delete_not_implemented(mock_faiss, fake_index, tmp_path):
    from custom_app.services.vectorstore.faiss_store import FaissVectorStore

    index_path = tmp_path / "test.index"
    index_path.write_bytes(b"fake")
    mock_faiss.read_index.return_value = fake_index

    store = FaissVectorStore.load(index_path, ["a", "b", "c"])
    with pytest.raises(NotImplementedError):
        store.upsert(["x"], np.zeros((1, 4), dtype="float32"))
    with pytest.raises(NotImplementedError):
        store.delete(["x"])


def test_protocol_compliance(mock_faiss, fake_index, tmp_path):
    """FaissVectorStore 必须符合 VectorStore Protocol（运行时可检查）。"""
    from custom_app.services.vectorstore.base import VectorStore
    from custom_app.services.vectorstore.faiss_store import FaissVectorStore

    index_path = tmp_path / "test.index"
    index_path.write_bytes(b"fake")
    mock_faiss.read_index.return_value = fake_index

    store = FaissVectorStore.load(index_path, ["a", "b", "c"])
    assert isinstance(store, VectorStore)


def test_chunk_ids_view_is_a_copy(mock_faiss, fake_index, tmp_path):
    """chunk_ids 属性返回副本，外部修改不影响内部状态。"""
    from custom_app.services.vectorstore.faiss_store import FaissVectorStore

    index_path = tmp_path / "test.index"
    index_path.write_bytes(b"fake")
    mock_faiss.read_index.return_value = fake_index

    store = FaissVectorStore.load(index_path, ["a", "b", "c"])
    ids = store.chunk_ids
    ids.append("HACKED")
    assert store.chunk_ids == ["a", "b", "c"]
