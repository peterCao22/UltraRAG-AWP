"""Phase P：流式对话分阶段耗时（profile）—— TDD 契约。"""

from unittest.mock import MagicMock, patch

import pytest

from custom_app.services.rag_runner import RagRunner


def _minimal_prep() -> dict:
    return {
        "q": "q",
        "rewritten_q": "rq",
        "hit_ids": [],
        "prompt_text": "prompt",
        "rerank_meta": {},
        "expanded_docs": [],
        "recall_k": 1,
        "final_k": 1,
        "final_k_cfg": 0,
    }


def _minimal_result() -> dict:
    return {
        "answer": "ok",
        "answer_blocks": [],
        "sources": [],
        "rewrite_query": "rq",
        "meta": {"retrieval_source_count": 0},
    }


def test_chat_stream_without_profile_omits_phase_timings_ms():
    r = RagRunner.__new__(RagRunner)
    r.kb_id = "t"

    with (
        patch.object(r, "_prepare_chat_context", return_value=_minimal_prep()),
        patch.object(r, "_generate_stream", return_value=iter(["x"])),
        patch.object(r, "_build_result_from_raw", return_value=_minimal_result()),
    ):
        events = list(r.chat_stream("hi", profile=False))

    meta = [e for e in events if e.get("type") == "meta"][0]
    assert "phase_timings_ms" not in meta


def test_chat_stream_with_profile_includes_phase_timings_ms():
    r = RagRunner.__new__(RagRunner)
    r.kb_id = "t"

    with (
        patch.object(r, "_prepare_chat_context", return_value=_minimal_prep()),
        patch.object(r, "_generate_stream", return_value=iter(["a", "b"])),
        patch.object(r, "_build_result_from_raw", return_value=_minimal_result()),
    ):
        events = list(r.chat_stream("hi", profile=True))

    meta = [e for e in events if e.get("type") == "meta"][0]
    assert "phase_timings_ms" in meta
    pt = meta["phase_timings_ms"]
    assert "prepare_context_ms" in pt
    assert "first_token_ms" in pt
    assert "generate_stream_total_ms" in pt
    assert pt["prepare_context_ms"] >= 0
    assert pt["first_token_ms"] >= 0
    assert pt["generate_stream_total_ms"] >= pt["first_token_ms"]


def test_chat_stream_profile_false_by_default():
    r = RagRunner.__new__(RagRunner)
    r.kb_id = "t"

    with (
        patch.object(r, "_prepare_chat_context", return_value=_minimal_prep()),
        patch.object(r, "_generate_stream", return_value=iter(["x"])),
        patch.object(r, "_build_result_from_raw", return_value=_minimal_result()),
    ):
        events = list(r.chat_stream("hi"))

    meta = [e for e in events if e.get("type") == "meta"][0]
    assert "phase_timings_ms" not in meta
