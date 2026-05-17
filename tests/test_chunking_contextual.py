"""Phase 8.2.1 Contextual chunking 单元测试。

策略：mock GeminiLLMAdapter.call 与 gemini_response_extract_text，
不真打 Gemini API；验证：
    - 单 chunk 生成成功 → context 写回 chunks.jsonl
    - 失败降级 → context="" 不抛错（PLAN §五.5）
    - 幂等：已有非空 context 的 chunk 默认跳过；force=True 时重生
    - 多 doc 聚合：按 doc 字段把多 chunk 拼成「整篇文档」给 Gemini
    - 图片占位行 [IMG: path] 自动剥离
    - 文档过长自动截断（_max_doc_chars 保护）
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from custom_app.services.chunking.contextual import (
    ContextEnricher,
    ContextResult,
    USER_TEMPLATE,
    _strip_image_placeholders,
)
from custom_app.services.google_embedder import compose_doc_embedding_text


# ─────────────────────────────────────────────────────────────────────────────
# 纯函数
# ─────────────────────────────────────────────────────────────────────────────


class TestStripImagePlaceholders:
    def test_removes_img_lines(self) -> None:
        text = "STEP 1\n[IMG: images/foo.png]\n按下按钮\n[IMG: images/bar.png]"
        assert _strip_image_placeholders(text) == "STEP 1\n按下按钮"

    def test_preserves_text_only(self) -> None:
        assert _strip_image_placeholders("hello\nworld") == "hello\nworld"

    def test_empty_input(self) -> None:
        assert _strip_image_placeholders("") == ""
        assert _strip_image_placeholders(None) == ""  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────────
# compose_doc_embedding_text 含 context 拼接（embedder 集成点）
# ─────────────────────────────────────────────────────────────────────────────


class TestEmbedderContextPrefix:
    def _row(self, **overrides: object) -> dict:
        row: dict = {
            "id": "agv_demo_step_1",
            "title": "AGV 启动手册 | STEP 1",
            "contents": "检查电池电量与急停按钮",
            "structure": {"heading_path": ["AGV 启动手册"]},
        }
        row.update(overrides)
        return row

    def test_without_context_keeps_old_behavior(self) -> None:
        """无 context 字段时与 Phase 4.3 行为完全一致。"""
        text = compose_doc_embedding_text(self._row())
        assert text.startswith("AGV 启动手册\nAGV 启动手册 | STEP 1\n")
        assert "检查电池电量" in text

    def test_context_prepended_when_present(self) -> None:
        ctx = "本文档介绍 XYZ 型号 AGV 启动流程，共 8 步；STEP 1 是开机前检查。"
        text = compose_doc_embedding_text(self._row(context=ctx))
        lines = text.split("\n")
        assert lines[0] == ctx
        assert lines[1] == "AGV 启动手册"  # heading_path 紧随
        assert "检查电池电量" in text

    def test_empty_context_falls_back(self) -> None:
        text = compose_doc_embedding_text(self._row(context=""))
        # 与无 context 等价
        assert not text.startswith(" ")
        assert text == compose_doc_embedding_text(self._row())

    def test_whitespace_only_context_treated_as_empty(self) -> None:
        text = compose_doc_embedding_text(self._row(context="   \n   "))
        assert text == compose_doc_embedding_text(self._row())


# ─────────────────────────────────────────────────────────────────────────────
# ContextEnricher 集成（mock Gemini）
# ─────────────────────────────────────────────────────────────────────────────


def _mock_adapter_response(text: str) -> dict:
    """构造 GeminiLLMAdapter.call() 兼容的返回结构。"""
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def _build_chunks_file(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "chunks.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


@pytest.fixture()
def _gemini_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key-for-test")
    monkeypatch.setenv("ULTRARAG_GEMINI_MODEL", "gemini-2.0-flash")


class TestContextEnricherBasic:
    def test_generates_context_for_all_chunks(
        self, tmp_path: Path, _gemini_env: None
    ) -> None:
        chunks = [
            {"id": "doc1_step_1", "doc": "doc1", "contents": "STEP 1: 启动主机"},
            {"id": "doc1_step_2", "doc": "doc1", "contents": "STEP 2: 启动外设"},
        ]
        path = _build_chunks_file(tmp_path, chunks)

        with patch(
            "custom_app.services.chunking.contextual.GeminiLLMAdapter"
        ) as MockAdapter:
            instance = MockAdapter.return_value
            instance.call.return_value = _mock_adapter_response(
                "本文档共 2 步启动流程；该 chunk 是其中之一。"
            )
            enricher = ContextEnricher(max_workers=1)
            n_gen, n_skip, n_fail = enricher.enrich_chunks_jsonl(path)

        assert (n_gen, n_skip, n_fail) == (2, 0, 0)
        # 读回校验 context 已写入
        written = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
        assert all((r.get("context") or "").strip() for r in written)

    def test_skip_chunks_with_existing_context(
        self, tmp_path: Path, _gemini_env: None
    ) -> None:
        chunks = [
            {"id": "a", "doc": "d", "contents": "x", "context": "已有摘要"},
            {"id": "b", "doc": "d", "contents": "y"},
        ]
        path = _build_chunks_file(tmp_path, chunks)
        with patch(
            "custom_app.services.chunking.contextual.GeminiLLMAdapter"
        ) as MockAdapter:
            instance = MockAdapter.return_value
            instance.call.return_value = _mock_adapter_response("新摘要")
            enricher = ContextEnricher(max_workers=1)
            n_gen, n_skip, n_fail = enricher.enrich_chunks_jsonl(path)

        assert (n_gen, n_skip, n_fail) == (1, 1, 0)
        written = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
        # chunk a 保留原 context；chunk b 新生成
        a_ctx = next(r["context"] for r in written if r["id"] == "a")
        b_ctx = next(r["context"] for r in written if r["id"] == "b")
        assert a_ctx == "已有摘要"
        assert b_ctx == "新摘要"

    def test_force_regenerates_all(self, tmp_path: Path, _gemini_env: None) -> None:
        chunks = [
            {"id": "a", "doc": "d", "contents": "x", "context": "旧"},
        ]
        path = _build_chunks_file(tmp_path, chunks)
        with patch(
            "custom_app.services.chunking.contextual.GeminiLLMAdapter"
        ) as MockAdapter:
            instance = MockAdapter.return_value
            instance.call.return_value = _mock_adapter_response("新")
            enricher = ContextEnricher(max_workers=1)
            n_gen, n_skip, n_fail = enricher.enrich_chunks_jsonl(path, force=True)

        assert (n_gen, n_skip, n_fail) == (1, 0, 0)
        written = json.loads(path.read_text(encoding="utf-8").strip())
        assert written["context"] == "新"


class TestContextEnricherDegradation:
    """PLAN §五.5 共识：失败降级 + 日志，不阻塞 ingest。"""

    def test_gemini_unavailable_yields_empty_context(
        self, tmp_path: Path, _gemini_env: None
    ) -> None:
        from custom_app.services.llm_adapter import GeminiServiceUnavailable

        chunks = [{"id": "a", "doc": "d", "contents": "x"}]
        path = _build_chunks_file(tmp_path, chunks)
        with patch(
            "custom_app.services.chunking.contextual.GeminiLLMAdapter"
        ) as MockAdapter:
            instance = MockAdapter.return_value
            instance.call.side_effect = GeminiServiceUnavailable("network down")
            enricher = ContextEnricher(max_workers=1)
            n_gen, n_skip, n_fail = enricher.enrich_chunks_jsonl(path)

        assert (n_gen, n_skip, n_fail) == (0, 0, 1)
        written = json.loads(path.read_text(encoding="utf-8").strip())
        assert written["context"] == ""  # 降级到空，未抛错

    def test_arbitrary_exception_yields_empty_context(
        self, tmp_path: Path, _gemini_env: None
    ) -> None:
        chunks = [{"id": "a", "doc": "d", "contents": "x"}]
        path = _build_chunks_file(tmp_path, chunks)
        with patch(
            "custom_app.services.chunking.contextual.GeminiLLMAdapter"
        ) as MockAdapter:
            instance = MockAdapter.return_value
            instance.call.side_effect = ValueError("bad prompt")
            enricher = ContextEnricher(max_workers=1)
            n_gen, n_skip, n_fail = enricher.enrich_chunks_jsonl(path)

        assert (n_gen, n_skip, n_fail) == (0, 0, 1)


class TestContextEnricherInternals:
    def test_build_full_docs_strips_image_placeholders(
        self, _gemini_env: None
    ) -> None:
        enricher = ContextEnricher()
        rows = [
            {"id": "a1", "doc": "A", "contents": "STEP 1\n[IMG: x.png]\n按下按钮"},
            {"id": "a2", "doc": "A", "contents": "STEP 2\n确认状态"},
            {"id": "b1", "doc": "B", "contents": "本文档不同"},
        ]
        out = enricher._build_full_docs(rows)
        assert set(out) == {"A", "B"}
        # A 文档应包含两段、无 [IMG: ...] 行
        assert "STEP 1" in out["A"] and "STEP 2" in out["A"]
        assert "[IMG" not in out["A"]
        # 不同 doc 不应混
        assert "本文档不同" not in out["A"]
        assert "STEP 1" not in out["B"]

    def test_build_full_docs_respects_max_doc_chars(self, _gemini_env: None) -> None:
        enricher = ContextEnricher(max_doc_chars=20)
        rows = [{"id": "x", "doc": "long", "contents": "甲" * 50}]
        out = enricher._build_full_docs(rows)
        assert len(out["long"]) == 20

    def test_generate_one_skips_empty_chunk(self, _gemini_env: None) -> None:
        enricher = ContextEnricher()
        row = {"id": "empty", "doc": "d", "contents": "   "}
        res = enricher._generate_one(row, "some doc text")
        assert isinstance(res, ContextResult)
        assert res.context == ""
        assert res.error == "empty chunk body"


def test_user_template_substitutes() -> None:
    """USER_TEMPLATE 必须能填入两个占位符（保护 prompt 不被无声破坏）。"""
    out = USER_TEMPLATE.format(full_document_text="DOC", chunk_contents="CHUNK")
    assert "DOC" in out and "CHUNK" in out
