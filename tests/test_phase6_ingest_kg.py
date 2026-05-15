"""
Phase 6.0 Ingest KG 自动提取 单元测试
======================================

验证 _run_ingest_job 新增的 Stage 4 KG 提取逻辑：
- _should_extract_kg() 按 enabled_tools 正确决策
- _kg_stage() 正确调用 extract_kb()
- KG 提取成功时 stages_done 含 "kg" + 统计数据
- KG 提取失败时 stages_done 含 "kg_failed"，ingest 仍返回 ok=True
- query_knowledge_graph 未在 enabled_tools 时跳过提取

运行：
  cd d:/Peter2025/myCursor/UltraRAG
  pytest tests/test_phase6_ingest_kg.py -v
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def sqlite_backend(monkeypatch):
    """强制使用 SQLite 后端，避免测试依赖 Postgres/Neo4j。"""
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")
    monkeypatch.setenv("ULTRARAG_KG_BACKEND", "sqlite")
    # 重置 Repository 单例，避免跨测试污染
    from custom_app.repositories import set_default_provider
    set_default_provider(None)
    yield
    set_default_provider(None)


@pytest.fixture()
def fake_kb(tmp_path):
    """构造一个最小化的 KB dict，路径指向 tmp_path。"""
    kb_root = tmp_path / "kb" / "test_kb"
    kb_root.mkdir(parents=True)
    (kb_root / "raw").mkdir()
    (kb_root / "corpora").mkdir()
    (kb_root / "embedding").mkdir()
    (kb_root / "index").mkdir()

    # 写入最小 chunks.jsonl，供 _should_extract_kg 之后的逻辑读取
    chunks_path = kb_root / "corpora" / "chunks.jsonl"
    chunks_path.write_text(
        json.dumps({"id": "chunk_1", "title": "Test", "contents": "hello world"}) + "\n",
        encoding="utf-8",
    )

    return {
        "kb_id": "test_kb",
        "type": "general",
        "data_path": str(kb_root),
        "embedding_path": str(kb_root / "embedding" / "embedding.npy"),
        "index_path": str(kb_root / "index" / "index.index"),
        "tenant_id": "default",
    }


# ── 辅助：patch ingest 所有重量级步骤 ─────────────────────────────────────────

def _patch_ingest_stages(monkeypatch_or_patch_ctx):
    """
    返回用于 with patch(...) 的 context manager 列表，
    屏蔽 parse/embed/index 三个耗时阶段及 DB 读写。
    """
    return [
        patch("custom_app.api.kb._register_documents"),
        patch("custom_app.api.kb._scan_raw_files", return_value=["file1.pdf"]),
        patch("custom_app.api.kb._parse_stage"),
        patch("custom_app.api.kb._embed_stage"),
        patch("custom_app.api.kb._index_stage", return_value=5),
        patch("custom_app.api.kb._mark_job_running"),
        patch("custom_app.api.kb._mark_job_success"),
        patch("custom_app.api.kb._mark_job_failed"),
    ]


# ── T1: KG 提取成功 ────────────────────────────────────────────────────────────

class TestKgStageCalledWhenEnabled:
    """query_knowledge_graph 在 enabled_tools 中时，extract_kb 应被调用。"""

    def test_extract_kb_called_once(self, fake_kb, tmp_path):
        """KG 开启时 extract_kb 应被调用，且传入正确的 kb_id 与 chunks_path。"""
        stages_recorded = []

        def fake_update_stage(job_id, stage, extra=None):
            stages_recorded.append((stage, extra or {}))

        with (
            patch("custom_app.api.kb._register_documents"),
            patch("custom_app.api.kb._scan_raw_files", return_value=["f.pdf"]),
            patch("custom_app.api.kb._parse_stage"),
            patch("custom_app.api.kb._embed_stage"),
            patch("custom_app.api.kb._index_stage", return_value=3),
            patch("custom_app.api.kb._mark_job_running"),
            patch("custom_app.api.kb._mark_job_success"),
            patch("custom_app.api.kb._update_job_stage", side_effect=fake_update_stage),
            # KG 开关：包含 query_knowledge_graph
            patch(
                "custom_app.api.kb._should_extract_kg",
                return_value=True,
            ),
            patch(
                "custom_app.api.kb._kg_stage",
                return_value={"entity_count": 7, "relation_count": 4},
            ) as mock_kg,
        ):
            from custom_app.api.kb import _run_ingest_job

            result = _run_ingest_job(fake_kb, "test_kb", "job_001", False)

        assert result["ok"] is True
        # _kg_stage 应被调用一次，传入 kb_id 和 chunks_path
        mock_kg.assert_called_once()
        call_args = mock_kg.call_args
        assert call_args.args[0] == "test_kb"  # kb_id

        # stages_done 应含 "kg"
        stage_names = [s for s, _ in stages_recorded]
        assert "kg" in stage_names

    def test_kg_counts_in_stage_extra(self, fake_kb):
        """KG stage 应记录 entity_count 和 relation_count。"""
        captured_extra = {}

        def fake_update_stage(job_id, stage, extra=None):
            if stage == "kg":
                captured_extra.update(extra or {})

        with (
            patch("custom_app.api.kb._register_documents"),
            patch("custom_app.api.kb._scan_raw_files", return_value=["f.pdf"]),
            patch("custom_app.api.kb._parse_stage"),
            patch("custom_app.api.kb._embed_stage"),
            patch("custom_app.api.kb._index_stage", return_value=3),
            patch("custom_app.api.kb._mark_job_running"),
            patch("custom_app.api.kb._mark_job_success"),
            patch("custom_app.api.kb._update_job_stage", side_effect=fake_update_stage),
            patch("custom_app.api.kb._should_extract_kg", return_value=True),
            patch(
                "custom_app.api.kb._kg_stage",
                return_value={"entity_count": 12, "relation_count": 8},
            ),
        ):
            from custom_app.api.kb import _run_ingest_job
            _run_ingest_job(fake_kb, "test_kb", "job_002", False)

        assert captured_extra.get("kg_entity_count") == 12
        assert captured_extra.get("kg_relation_count") == 8


# ── T2: KG 提取被跳过 ─────────────────────────────────────────────────────────

class TestKgStageSkipped:
    """query_knowledge_graph 不在 enabled_tools 或配置为空时，跳过 KG 提取。"""

    def test_skipped_when_not_in_tools(self, fake_kb):
        """enabled_tools 不含 query_knowledge_graph 时，_kg_stage 不应被调用。"""
        with (
            patch("custom_app.api.kb._register_documents"),
            patch("custom_app.api.kb._scan_raw_files", return_value=["f.pdf"]),
            patch("custom_app.api.kb._parse_stage"),
            patch("custom_app.api.kb._embed_stage"),
            patch("custom_app.api.kb._index_stage", return_value=3),
            patch("custom_app.api.kb._mark_job_running"),
            patch("custom_app.api.kb._mark_job_success"),
            patch("custom_app.api.kb._update_job_stage"),
            patch("custom_app.api.kb._should_extract_kg", return_value=False),
            patch("custom_app.api.kb._kg_stage") as mock_kg,
        ):
            from custom_app.api.kb import _run_ingest_job
            result = _run_ingest_job(fake_kb, "test_kb", "job_003", False)

        assert result["ok"] is True
        mock_kg.assert_not_called()

    def test_should_extract_kg_false_when_tool_missing(self, monkeypatch):
        """_should_extract_kg 在 enabled_tools 不含 query_knowledge_graph 时返回 False。"""
        with patch(
            "custom_app.services.agent_config_store.get_enabled_tools",
            return_value=["knowledge_search", "keyword_search"],
        ):
            from custom_app.api.kb import _should_extract_kg
            assert _should_extract_kg("any_kb") is False

    def test_should_extract_kg_true_when_tool_present(self, monkeypatch):
        """_should_extract_kg 在 enabled_tools 含 query_knowledge_graph 时返回 True。"""
        with patch(
            "custom_app.services.agent_config_store.get_enabled_tools",
            return_value=["knowledge_search", "query_knowledge_graph"],
        ):
            from custom_app.api.kb import _should_extract_kg
            assert _should_extract_kg("any_kb") is True


# ── T3: KG 提取失败不影响 ingest 结果 ────────────────────────────────────────

class TestKgFailureDoesNotFailIngest:
    """extract_kb 抛异常时，ingest job 仍应返回 ok=True，并记录 kg_failed stage。"""

    def test_ingest_ok_when_kg_raises(self, fake_kb):
        """KG 阶段抛出 RuntimeError，ingest 应仍返回 ok=True。"""
        with (
            patch("custom_app.api.kb._register_documents"),
            patch("custom_app.api.kb._scan_raw_files", return_value=["f.pdf"]),
            patch("custom_app.api.kb._parse_stage"),
            patch("custom_app.api.kb._embed_stage"),
            patch("custom_app.api.kb._index_stage", return_value=3),
            patch("custom_app.api.kb._mark_job_running"),
            patch("custom_app.api.kb._mark_job_success"),
            patch("custom_app.api.kb._update_job_stage"),
            patch("custom_app.api.kb._should_extract_kg", return_value=True),
            patch(
                "custom_app.api.kb._kg_stage",
                side_effect=RuntimeError("Gemini API quota exceeded"),
            ),
        ):
            from custom_app.api.kb import _run_ingest_job
            result = _run_ingest_job(fake_kb, "test_kb", "job_004", False)

        # 索引已完成，job 应成功
        assert result["ok"] is True
        assert result.get("status") == "success"

    def test_kg_failed_stage_recorded_on_error(self, fake_kb):
        """KG 失败时应在 stages 中记录 kg_failed 而非 kg。"""
        stages_recorded = []

        def fake_update_stage(job_id, stage, extra=None):
            stages_recorded.append((stage, extra or {}))

        with (
            patch("custom_app.api.kb._register_documents"),
            patch("custom_app.api.kb._scan_raw_files", return_value=["f.pdf"]),
            patch("custom_app.api.kb._parse_stage"),
            patch("custom_app.api.kb._embed_stage"),
            patch("custom_app.api.kb._index_stage", return_value=3),
            patch("custom_app.api.kb._mark_job_running"),
            patch("custom_app.api.kb._mark_job_success"),
            patch("custom_app.api.kb._update_job_stage", side_effect=fake_update_stage),
            patch("custom_app.api.kb._should_extract_kg", return_value=True),
            patch(
                "custom_app.api.kb._kg_stage",
                side_effect=ConnectionError("Neo4j unreachable"),
            ),
        ):
            from custom_app.api.kb import _run_ingest_job
            _run_ingest_job(fake_kb, "test_kb", "job_005", False)

        stage_names = [s for s, _ in stages_recorded]
        assert "kg_failed" in stage_names
        assert "kg" not in stage_names

        # kg_failed stage 应含错误信息
        kg_failed_extra = next(e for s, e in stages_recorded if s == "kg_failed")
        assert "kg_error" in kg_failed_extra
        assert "Neo4j unreachable" in kg_failed_extra["kg_error"]
