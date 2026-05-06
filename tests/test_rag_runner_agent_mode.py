"""RagRunner：agent_mode 层 A 全文扩展与 meta 契约。"""

from unittest.mock import MagicMock

import numpy as np
import pytest

from custom_app.services.rag_runner import RagRunner


@pytest.fixture()
def runner_rows():
    r = RagRunner.__new__(RagRunner)
    r._rows = [
        {"id": "d1_intro", "doc": "DocA", "title": "DocA | intro", "contents": "intro"},
        {"id": "d1_s1", "doc": "DocA", "title": "DocA | STEP 1", "contents": "step1"},
        {"id": "d1_s2", "doc": "DocA", "title": "DocA | STEP 2", "contents": "step2"},
        {"id": "d2_only", "doc": "DocB", "title": "DocB | x", "contents": "other"},
        {"id": "no_doc", "doc": "", "title": "orphan", "contents": "x"},
    ]
    return r


def test_expand_quick_no_procedure_skips_full_doc(runner_rows):
    r = runner_rows
    hit_ids, expanded = r._expand_hit_ids([0], "generic question", agent_mode="quick")
    assert expanded == []
    assert hit_ids == [0]


def test_expand_agent_pulls_full_primary_doc(runner_rows):
    r = runner_rows
    hit_ids, expanded = r._expand_hit_ids([0], "generic question", agent_mode="agent")
    assert expanded == ["DocA"]
    assert set(hit_ids) == {0, 1, 2}


def test_expand_agent_multi_doc_narrows_to_primary(runner_rows):
    r = runner_rows
    hit_ids, expanded = r._expand_hit_ids([0, 3], "generic question", agent_mode="agent")
    assert expanded == ["DocA"]
    assert set(hit_ids) == {0, 1, 2}


def test_prepare_agent_degraded_when_no_doc_on_hits(runner_rows, monkeypatch):
    """向量仅命中无 doc 字段的 chunk 时，agent 无法做全文扩展，应标记 degraded。"""
    r = runner_rows
    r._index = MagicMock()
    r._index.search.return_value = (None, np.array([[4]], dtype="int64"))
    r._top_k = 8
    r._recall_top_k = 4
    r._final_top_k = 0
    r._rerank_cfg = {}
    r._rerank_model = None
    r._rewrite_query = lambda q: q

    monkeypatch.setattr(
        "custom_app.services.rag_runner.embed_query",
        lambda q: np.zeros((1, 4), dtype="float32"),
    )
    monkeypatch.setattr(r, "_build_prompt", lambda q, ids: "prompt")

    prep = r._prepare_chat_context("hi", agent_mode="agent")
    assert prep["degraded"] is True
    assert prep["effective_agent_mode"] == "quick"


def test_build_result_merges_agent_meta(runner_rows):
    r = runner_rows
    prep = {
        "q": "q",
        "rewritten_q": "q",
        "hit_ids": [0],
        "prompt_text": "p",
        "rerank_meta": {},
        "expanded_docs": ["DocA"],
        "recall_k": 1,
        "final_k": 3,
        "final_k_cfg": 0,
        "requested_agent_mode": "agent",
        "effective_agent_mode": "agent",
        "degraded": False,
        "degrade_reason": None,
    }
    out = r._build_result_from_raw(prep, "raw")
    meta = out["meta"]
    assert meta["effective_agent_mode"] == "agent"
    assert meta["degraded"] is False
