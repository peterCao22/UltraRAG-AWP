"""Phase 4.0 — docx_parser 新 schema 字段验证。

确保 docx_parser 输出的 chunk 在保留老字段的同时，正确填入 Phase 4 新 schema：
    - source_type / parser
    - structure.heading_path / heading_level / step_number / page_idx
    - tables / vector_id
"""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_app.services.docx_parser import parse_directory
from custom_app.services.parsers.schema import Chunk


@pytest.fixture(scope="module")
def agv_chunks() -> list[dict]:
    """用 data/kb/agv_demo 真实 SOP 文档跑一次解析。"""
    raw_dir = Path("data/kb/agv_demo/raw")
    kb_root = Path("data/kb/agv_demo")
    if not raw_dir.exists():
        pytest.skip(f"sample directory missing: {raw_dir}")
    chunks = parse_directory(raw_dir, kb_root)
    if not chunks:
        pytest.skip("no chunks parsed from sample directory")
    return chunks


def test_legacy_fields_preserved(agv_chunks: list[dict]) -> None:
    """老字段必须完整保留，类型不变。"""
    for c in agv_chunks:
        assert isinstance(c["id"], str) and c["id"]
        assert isinstance(c["title"], str)
        assert isinstance(c["contents"], str)
        assert isinstance(c["doc"], str) and c["doc"]
        assert isinstance(c["images"], list)
        for img in c["images"]:
            assert isinstance(img, str), (
                "docx_parser 应继续输出字符串数组以兼容 api/chat.py 与 list_chunks.py；"
                "对象数组留给 mineru/docling parser"
            )


def test_new_schema_fields_present(agv_chunks: list[dict]) -> None:
    """新 schema 字段必须存在且类型正确。"""
    for c in agv_chunks:
        assert c["source_type"] == "sop_docx"
        assert c["parser"] == "docx_parser"
        assert c["tables"] == []
        assert c["vector_id"] is None

        struct = c["structure"]
        assert isinstance(struct, dict)
        assert isinstance(struct["heading_path"], list)
        assert isinstance(struct["heading_level"], int)
        # step_number 仅 STEP chunk 有；intro/section chunk 应为 None
        assert struct["step_number"] is None or isinstance(struct["step_number"], int)
        assert struct["page_idx"] is None


def test_step_chunks_have_step_number(agv_chunks: list[dict]) -> None:
    """以 _step_<N> 结尾的 chunk 必须有 step_number 字段。"""
    step_chunks = [c for c in agv_chunks if "_step_" in c["id"]]
    if not step_chunks:
        pytest.skip("no STEP chunks in sample data")
    for c in step_chunks:
        sn = c["structure"]["step_number"]
        assert sn is not None and sn > 0, (
            f"STEP chunk {c['id']} should have positive step_number, got {sn}"
        )


def test_intro_chunks_have_no_step_number(agv_chunks: list[dict]) -> None:
    """_intro / _section_<N> chunk 不应该有 step_number。"""
    non_step = [c for c in agv_chunks if "_step_" not in c["id"]]
    for c in non_step:
        assert c["structure"]["step_number"] is None, (
            f"non-STEP chunk {c['id']} should not have step_number"
        )


def test_chunk_from_jsonl_dict_roundtrip(agv_chunks: list[dict]) -> None:
    """新 schema dict 必须可被 Chunk.from_jsonl_dict 重建。"""
    for c in agv_chunks[:5]:  # 抽 5 个即可，全跑太慢
        rebuilt = Chunk.from_jsonl_dict(c)
        assert rebuilt.id == c["id"]
        assert rebuilt.source_type == "sop_docx"
        assert rebuilt.parser == "docx_parser"
        assert list(rebuilt.structure.heading_path) == c["structure"]["heading_path"]
        # images 老格式（字符串数组）也能被 from_jsonl_dict 接受
        assert len(rebuilt.images) == len(c["images"])
