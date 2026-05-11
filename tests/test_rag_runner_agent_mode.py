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


def test_keyword_match_finds_mixed_language_alarm_name():
    r = RagRunner.__new__(RagRunner)
    r._rows = [
        {
            "id": "other",
            "doc": "Other SOP",
            "title": "Other SOP",
            "contents": "Some unrelated content",
        },
        {
            "id": "estop",
            "doc": "E-Stop SOP",
            "title": "E-Stop SOP",
            "contents": "Alarm: ID 01 E-Stop Button Active\nHow To Fix: Check both E-stop buttons.",
        },
    ]

    hits = r._keyword_match_hit_ids("E-Stop Button Active 的故障如何恢复")

    assert hits == [1]


def test_merge_preferred_hit_ids_keeps_keyword_hits_first():
    merged = RagRunner._merge_preferred_hit_ids([3, 1], [1, 2, 3, 4])

    assert merged == [3, 1, 2, 4]


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


def test_quick_chat_stream_uses_non_streaming_generation(runner_rows):
    r = runner_rows
    r._index = MagicMock()
    r._index.search.return_value = (None, np.array([[0]], dtype="int64"))
    r._top_k = 1
    r._recall_top_k = 1
    r._final_top_k = 0
    r._rerank_cfg = {}
    r._rerank_model = None
    r._rewrite_query = lambda q: q
    r._build_prompt = lambda q, ids: "prompt"
    r._generate = MagicMock(return_value="raw answer")
    r._generate_stream = MagicMock(side_effect=AssertionError("stream should not be used"))
    r._build_result_from_raw = MagicMock(return_value={
        "answer": "display answer",
        "sources": [],
        "rewrite_query": "q",
        "meta": {},
    })

    import custom_app.services.rag_runner as rag_runner_mod
    old_embed = rag_runner_mod.embed_query
    rag_runner_mod.embed_query = lambda q: np.zeros((1, 4), dtype="float32")
    try:
        events = list(r.chat_stream("q", agent_mode="quick"))
    finally:
        rag_runner_mod.embed_query = old_embed

    assert any(ev.get("type") == "chunk" and ev.get("content") == "display answer" for ev in events)
    r._generate.assert_called_once_with("prompt")
    r._generate_stream.assert_not_called()


def test_generation_backend_accepts_backend_alias(tmp_path):
    r = RagRunner.__new__(RagRunner)
    r._chat_cfg = {}
    r._apply_ultrarag_generation_env_overrides = lambda: None
    r._gemini_model_id = lambda: "gemini-test"

    kb_dir = tmp_path / "data" / "kb" / "demo"
    (kb_dir / "corpora").mkdir(parents=True)
    (kb_dir / "index").mkdir()
    (kb_dir / "corpora" / "chunks.jsonl").write_text('{"id":"1","title":"t","contents":"c","doc":"d"}\n', encoding="utf-8")
    (kb_dir / "index" / "index.index").write_bytes(b"idx")
    gen = tmp_path / "generation.yaml"
    gen.write_text(
        """
backend: gemini
backend_configs:
  openai:
    model_name: ignored
    base_url: http://unused/v1
sampling_params:
  max_tokens: 128
""",
        encoding="utf-8",
    )
    retriever = tmp_path / "retriever.yaml"
    retriever.write_text("{}", encoding="utf-8")

    import custom_app.services.rag_runner as rag_runner_mod
    old_faiss = rag_runner_mod.faiss
    rag_runner_mod.faiss = MagicMock()
    rag_runner_mod.faiss.read_index.return_value = object()
    try:
        r.__init__(
            kb_id="demo",
            kb_base_dir=str(tmp_path / "data" / "kb"),
            generation_param_path=str(gen),
            retriever_param_path=str(retriever),
        )
        r.init()
    finally:
        rag_runner_mod.faiss = old_faiss

    assert r._chat_cfg["backend"] == "gemini"
