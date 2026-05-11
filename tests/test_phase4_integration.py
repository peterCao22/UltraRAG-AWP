"""Phase 4 跨阶段集成测试（不依赖外部服务）。

覆盖以下集成点，确保 4.0~4.3 各模块组合后行为一致：

    parser → chunks.jsonl → from_jsonl_dict → compose_doc_embedding_text

具体场景：
    1. general KB 混合多种格式（md + md），parse_files 工厂 + jsonl 序列化往返
    2. SOP docx_parser 输出 + heading_path 嵌入增强
    3. 新 Chunk schema 写入 chunks.jsonl 后可被 from_jsonl_dict 完整重建
"""

from __future__ import annotations

import json
from pathlib import Path
from dataclasses import replace

import pytest

from custom_app.services.parsers import (
    KB_TYPE_GENERAL,
    Chunk,
    parse_files,
)
from custom_app.services.parsers.schema import (
    ChunkImage,
    ChunkStructure,
    ChunkTable,
)
from custom_app.services.google_embedder import compose_doc_embedding_text


# ---------------------------------------------------------------------------
# 端到端：general KB 多文件 → chunks → 嵌入文本
# ---------------------------------------------------------------------------


def test_general_kb_mixed_md_files_e2e(tmp_path: Path):
    """general KB 处理多个 markdown 文件 → 工厂统一输出 → 序列化 → 嵌入文本。"""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "故障手册.md").write_text(
        "# 第3章 故障处理\n章节引言\n\n## 3.1 电池告警\n告警处理流程。\n",
        encoding="utf-8",
    )
    (raw_dir / "维护指南.md").write_text(
        "# 日常维护\n维护步骤说明。\n",
        encoding="utf-8",
    )

    files = sorted(raw_dir.glob("*.md"))
    chunks = parse_files(KB_TYPE_GENERAL, files, tmp_path, kb_id="ops_kb")

    # 至少 3 个 chunk：故障手册有 2 个 section（intro + 3.1），维护指南有 1 个
    assert len(chunks) >= 3
    for c in chunks:
        assert isinstance(c, Chunk)
        assert c.kb_id == "ops_kb"
        assert c.source_type == "markdown"
        assert c.parser == "markdown"

    # 检查 heading_path 重建正确
    by_id = {c.id: c for c in chunks}
    fault_intro = next(
        c for c in chunks if c.doc == "故障手册" and c.structure.heading_level == 1
    )
    assert fault_intro.structure.heading_path == ("第3章 故障处理",)

    fault_battery = next(
        c for c in chunks if c.doc == "故障手册" and c.structure.heading_level == 2
    )
    assert fault_battery.structure.heading_path == ("第3章 故障处理", "3.1 电池告警")

    # 序列化 → 反序列化 round-trip
    for c in chunks:
        d = c.to_jsonl_dict()
        rebuilt = Chunk.from_jsonl_dict(d)
        assert rebuilt.id == c.id
        assert rebuilt.structure.heading_path == c.structure.heading_path
        assert rebuilt.source_type == c.source_type
        assert rebuilt.parser == c.parser

    # 嵌入文本应包含 heading_path 前缀
    embedding_text = compose_doc_embedding_text(fault_battery.to_jsonl_dict())
    assert "第3章 故障处理 > 3.1 电池告警" in embedding_text
    assert "告警处理流程" in embedding_text


def test_chunks_jsonl_full_pipeline(tmp_path: Path):
    """写入 chunks.jsonl，读回后逐行通过 compose_doc_embedding_text 处理。"""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "doc.md").write_text(
        "# A\n## B\n正文内容\n", encoding="utf-8"
    )

    chunks = parse_files(KB_TYPE_GENERAL, sorted(raw_dir.glob("*.md")), tmp_path)
    chunks_path = tmp_path / "chunks.jsonl"
    chunks_path.write_text(
        "\n".join(json.dumps(c.to_jsonl_dict(), ensure_ascii=False) for c in chunks),
        encoding="utf-8",
    )

    # 读回 + 计算嵌入文本
    rows = [
        json.loads(line)
        for line in chunks_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows
    for row in rows:
        text = compose_doc_embedding_text(row)
        # heading_path 前缀应在文本里
        if row["structure"]["heading_path"]:
            assert " > ".join(row["structure"]["heading_path"]) in text or row["structure"]["heading_path"][0] in text


# ---------------------------------------------------------------------------
# SOP DOCX → heading_path 嵌入增强（用真实样本）
# ---------------------------------------------------------------------------


def test_sop_docx_chunks_with_heading_embedding(tmp_path: Path):
    """SOP DOCX 解析 → 嵌入文本应含 heading_path 前缀（如果文档有 Heading 样式）。"""
    sample_dir = Path("data/kb/agv_demo/raw")
    if not sample_dir.exists():
        pytest.skip(f"sample dir missing: {sample_dir}")
    docx_files = list(sample_dir.glob("*.docx"))
    if not docx_files:
        pytest.skip("no SOP docx sample")

    from custom_app.services.docx_parser import parse_directory

    chunks = parse_directory(sample_dir, Path("data/kb/agv_demo"))
    assert chunks, "parse_directory should produce chunks"

    # 至少一部分 chunk 应该有非空 heading_path（来自 docx_parser 写入的 h_run）
    chunks_with_heading = [
        c for c in chunks
        if c.get("structure", {}).get("heading_path")
    ]
    assert chunks_with_heading, "SOP docx 至少应有部分 chunk 含 heading_path"

    # 验证这些 chunk 的嵌入文本含 heading 前缀
    sample = chunks_with_heading[0]
    text = compose_doc_embedding_text(sample)
    expected_prefix = sample["structure"]["heading_path"][0]
    assert expected_prefix in text


# ---------------------------------------------------------------------------
# 老 chunks.jsonl 零回归
# ---------------------------------------------------------------------------


def test_phase3_legacy_chunks_compatible():
    """Phase 3 chunks.jsonl（无 structure / 字符串 images）应通过 from_jsonl_dict 重建。"""
    legacy = {
        "id": "doc_step_1",
        "title": "STEP 1",
        "contents": "操作步骤",
        "doc": "doc",
        "images": ["images/doc/img_0001.png"],  # 字符串数组（Phase 3 格式）
    }
    chunk = Chunk.from_jsonl_dict(legacy)
    assert chunk.id == "doc_step_1"
    assert len(chunk.images) == 1
    assert chunk.images[0].path == "images/doc/img_0001.png"
    # 默认值
    assert chunk.source_type == "sop_docx"
    assert chunk.parser == "docx_parser"
    assert chunk.structure.heading_path == ()
    # 嵌入文本应退化到 Phase 3 行为
    text = compose_doc_embedding_text(legacy)
    assert text == "STEP 1\n操作步骤"


# ---------------------------------------------------------------------------
# 跨 parser 的 Chunk 都能被 compose_doc_embedding_text 处理
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_type,parser_name,heading_path,expected_prefix",
    [
        ("sop_docx", "docx_parser", ["第1章"], "第1章"),
        ("general_pdf", "mineru", ["A", "B"], "A > B"),
        ("general_docx", "docling", ["X"], "X"),
        ("markdown", "markdown", ["P", "Q", "R"], "P > Q > R"),
        ("image", "mineru", [], None),  # 无 heading_path
    ],
)
def test_compose_text_handles_all_parser_outputs(
    source_type, parser_name, heading_path, expected_prefix
):
    """无论哪个 parser 输出的 Chunk，嵌入拼接逻辑都一致。"""
    chunk = Chunk(
        id="x",
        title="标题",
        contents="正文",
        doc="d",
        source_type=source_type,
        parser=parser_name,
        structure=ChunkStructure(heading_path=tuple(heading_path)),
    )
    text = compose_doc_embedding_text(chunk.to_jsonl_dict())
    if expected_prefix:
        assert text.startswith(expected_prefix), f"missing prefix for {parser_name}"
        assert "标题" in text
        assert "正文" in text
    else:
        # 无 heading_path：等价 Phase 3 行为
        assert text == "标题\n正文"
