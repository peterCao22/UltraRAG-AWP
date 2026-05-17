"""Phase 8.2.1.c —— _context_stage 在 ingest pipeline 中的降级行为。

PLAN §五.5 共识：context 失败不阻塞 ingest。
本测试验证：
    - env ULTRARAG_DISABLE_CONTEXTUAL=1 时跳过整 stage
    - GOOGLE_API_KEY 未设时 ContextEnricher 初始化失败 → stage 降级而非抛错
    - ContextEnricher.enrich_chunks_jsonl 抛任意异常 → stage 降级
    - 正常路径：stage 返回 generated/skipped/failed 统计
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from custom_app.api.kb import _context_stage


@pytest.fixture()
def _chunks_file(tmp_path: Path) -> Path:
    path = tmp_path / "chunks.jsonl"
    rows = [{"id": "a", "doc": "d", "contents": "x"}]
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


class TestContextStageDegradation:
    def test_disabled_via_env(
        self, monkeypatch: pytest.MonkeyPatch, _chunks_file: Path
    ) -> None:
        monkeypatch.setenv("ULTRARAG_DISABLE_CONTEXTUAL", "1")
        stats = _context_stage(_chunks_file)
        assert stats.get("disabled") == 1
        assert stats["generated"] == 0

    def test_missing_api_key_degrades(
        self, monkeypatch: pytest.MonkeyPatch, _chunks_file: Path
    ) -> None:
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("ULTRARAG_GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("ULTRARAG_DISABLE_CONTEXTUAL", raising=False)

        # 防 dotenv 注入回 GOOGLE_API_KEY：替换 ContextEnricher 让构造直接抛 RuntimeError
        with patch(
            "custom_app.services.chunking.contextual.ContextEnricher.__init__",
            side_effect=RuntimeError("GOOGLE_API_KEY is not set"),
        ):
            stats = _context_stage(_chunks_file)
        assert stats.get("disabled") == 1
        assert stats["generated"] == 0
        assert stats["failed"] == 0  # 启动失败不计入"chunk 失败"

    def test_enrich_unexpected_exception_degrades(
        self, monkeypatch: pytest.MonkeyPatch, _chunks_file: Path
    ) -> None:
        monkeypatch.setenv("GOOGLE_API_KEY", "fake")
        monkeypatch.delenv("ULTRARAG_DISABLE_CONTEXTUAL", raising=False)
        with patch(
            "custom_app.services.chunking.contextual.ContextEnricher.enrich_chunks_jsonl",
            side_effect=RuntimeError("unexpected"),
        ):
            stats = _context_stage(_chunks_file)
        assert stats["failed"] == -1
        assert "error" in stats

    def test_happy_path_returns_counts(
        self, monkeypatch: pytest.MonkeyPatch, _chunks_file: Path
    ) -> None:
        monkeypatch.setenv("GOOGLE_API_KEY", "fake")
        monkeypatch.delenv("ULTRARAG_DISABLE_CONTEXTUAL", raising=False)
        with patch(
            "custom_app.services.chunking.contextual.ContextEnricher.enrich_chunks_jsonl",
            return_value=(3, 2, 1),
        ):
            stats = _context_stage(_chunks_file)
        assert stats == {"generated": 3, "skipped": 2, "failed": 1}
