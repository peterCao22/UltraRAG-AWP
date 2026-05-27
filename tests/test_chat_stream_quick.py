"""Phase 11.1.B —— quick mode 流式输出 + 重复 bug 修复回归测试。

策略：mock _prepare_chat_context + _generate_stream + _generate + _build_result_from_raw，
不真起 RagRunner.init。验证：
- quick mode 默认走 _generate_stream（多次 chunk 事件）
- 不在 done 之前发"完整 display_answer" chunk（修复 E.3 重复 bug）
- ULTRARAG_DISABLE_STREAM=1 时退到非流式（_generate）
- 流式失败时 fallback 到非流式 + 仍能推一次 chunk
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_app.services.rag_runner import RagRunner


def _make_runner_for_chat_stream(
    *,
    generate_stream_pieces: list[str] | None = None,
    generate_non_stream: str = "",
    build_result_answer: str = "DISPLAY_ANSWER",
    stream_should_raise: bool = False,
) -> RagRunner:
    """构造一个能跑 chat_stream 的最小 stub runner（不走 init）。"""
    runner = RagRunner.__new__(RagRunner)
    runner.kb_id = "test_kb"
    runner._rows = [{"id": "x"}]
    runner._chat_cfg = {"backend": "openai", "model_name": "stub"}
    # _normalize_agent_mode is real method, no need to stub

    # mock prepare/generate/build
    runner._prepare_chat_context = MagicMock(
        return_value={"hit_ids": [0], "prompt_text": "stub prompt", "degraded": False}
    )
    if stream_should_raise:
        runner._generate_stream = MagicMock(side_effect=RuntimeError("stream hang"))
    else:
        runner._generate_stream = MagicMock(
            return_value=iter(generate_stream_pieces or [])
        )
    runner._generate = MagicMock(return_value=generate_non_stream)
    runner._build_result_from_raw = MagicMock(
        return_value={
            "answer": build_result_answer,
            "answer_blocks": [],
            "sources": [],
            "rewrite_query": "stub",
            "meta": {},
        }
    )
    return runner


def _collect_events(runner: RagRunner, question: str = "Q?") -> list[dict]:
    return list(runner.chat_stream(question, agent_mode="quick"))


class TestQuickStreamHappyPath:
    def test_default_uses_stream_emits_pieces(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ULTRARAG_DISABLE_STREAM", raising=False)
        runner = _make_runner_for_chat_stream(
            generate_stream_pieces=["Hello ", "world", "!"],
            build_result_answer="Hello world!",
        )
        events = _collect_events(runner)
        chunks = [e for e in events if e.get("type") == "chunk"]
        # 3 pieces from stream，但**不**再补一次完整 display_answer
        assert [c["content"] for c in chunks] == ["Hello ", "world", "!"]
        runner._generate_stream.assert_called_once()
        runner._generate.assert_not_called()

    def test_no_duplicate_full_answer_chunk(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """E.3 回归：流式后不能在 done 之前再 yield 一次完整 display_answer。"""
        monkeypatch.delenv("ULTRARAG_DISABLE_STREAM", raising=False)
        runner = _make_runner_for_chat_stream(
            generate_stream_pieces=["abc"],
            build_result_answer="abc-formatted-with-images",
        )
        events = _collect_events(runner)
        chunk_contents = [
            e.get("content") for e in events if e.get("type") == "chunk"
        ]
        # 没有任何 chunk 等于 build_result_answer 的展示版本
        assert "abc-formatted-with-images" not in chunk_contents


class TestDisableStream:
    def test_env_disable_uses_non_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ULTRARAG_DISABLE_STREAM", "1")
        runner = _make_runner_for_chat_stream(
            generate_non_stream="non-stream-answer",
            build_result_answer="non-stream-answer-display",
        )
        events = _collect_events(runner)
        runner._generate_stream.assert_not_called()
        runner._generate.assert_called_once()
        # 非流式路径必须 yield 一次 display_answer chunk（前端拿不到任何 chunk 否则）
        chunk_contents = [
            e.get("content") for e in events if e.get("type") == "chunk"
        ]
        assert "non-stream-answer-display" in chunk_contents


class TestStreamFallback:
    def test_stream_failure_falls_back_to_non_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ULTRARAG_DISABLE_STREAM", raising=False)
        runner = _make_runner_for_chat_stream(
            generate_non_stream="fallback-answer",
            build_result_answer="fallback-display",
            stream_should_raise=True,
        )
        events = _collect_events(runner)
        runner._generate_stream.assert_called_once()
        # fallback 触发非流式
        runner._generate.assert_called_once()
        # fallback 后必须推一次 display_answer 让前端有内容
        chunk_contents = [
            e.get("content") for e in events if e.get("type") == "chunk"
        ]
        assert "fallback-display" in chunk_contents


class TestStreamEmits:
    def test_done_event_emitted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ULTRARAG_DISABLE_STREAM", raising=False)
        runner = _make_runner_for_chat_stream(
            generate_stream_pieces=["a"], build_result_answer="a-display",
        )
        events = _collect_events(runner)
        types = [e.get("type") for e in events]
        assert "done" in types
        # done 事件携带 display_answer 给前端做 Markdown 渲染（含图片 data URL）
        done = next(e for e in events if e.get("type") == "done")
        assert done.get("answer") == "a-display"
