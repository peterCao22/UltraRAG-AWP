"""Phase 4.2 — KB type 路由集成测试。

验证 Parser 工厂在 KB 入库阶段被正确调用：
    - sop_docx KB 走 docx_parser.parse_directory
    - general KB 走 parsers.parse_files 工厂（per-file 分发）

兼容性：custom_app/api/kb.py 顶层 import faiss；若 venv 缺 faiss，本测试模块顶部
预先注入 fake faiss module，让 import 不挂（这与现有
test_rag_runner_agent_mode.py 中的 mock 思路一致）。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 预注入 fake faiss / numpy / job_executor / google_embedder
# 让 kb.py 在无 faiss/Google API key 的 .venv 中也能 import
# ---------------------------------------------------------------------------
try:
    import faiss  # type: ignore
except ImportError:
    fake_faiss = types.ModuleType("faiss")
    fake_faiss.IndexFlatIP = MagicMock()
    fake_faiss.IndexIDMap2 = MagicMock()
    fake_faiss.read_index = MagicMock()
    fake_faiss.write_index = MagicMock()
    sys.modules["faiss"] = fake_faiss

# google_embedder 在 import 时会读取 Google API key；测试中我们不调用其函数，
# 但 import kb.py 时会 import 它，可能引起初始化失败。如果失败就替换为 stub。
try:
    from custom_app.services import google_embedder  # noqa: F401
except Exception:
    fake_embedder = types.ModuleType("custom_app.services.google_embedder")
    fake_embedder.build_embedding_npy = MagicMock()
    fake_embedder.embed_query = MagicMock()
    fake_embedder.embed_texts = MagicMock()
    sys.modules["custom_app.services.google_embedder"] = fake_embedder


import json  # noqa: E402 — must come after the faiss stub injection


def _make_fake_kb(kb_type: str, kb_id: str = "test_kb", tenant: str = "default") -> dict:
    return {
        "kb_id": kb_id,
        "tenant_id": tenant,
        "type": kb_type,
        "data_path": "/fake/data",
    }


# ---------------------------------------------------------------------------
# _kb_type / _scan_raw_files 行为
# ---------------------------------------------------------------------------


def test_scan_raw_files_sop_only_docx(tmp_path: Path):
    """sop_docx KB 只扫描 .docx；混在一起的 pdf/md 被忽略。"""
    from custom_app.api.kb import _scan_raw_files

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "a.docx").write_bytes(b"PK\x03\x04fake")
    (raw_dir / "b.pdf").write_bytes(b"%PDF-1.4 fake")
    (raw_dir / "c.md").write_text("# md", encoding="utf-8")

    kb = _make_fake_kb("sop_docx")
    files = _scan_raw_files(kb, raw_dir)
    assert [fp.name for fp in files] == ["a.docx"]


def test_scan_raw_files_general_accepts_all(tmp_path: Path):
    """general KB 接受 docx/pdf/md/图片 等多种扩展名。"""
    from custom_app.api.kb import _scan_raw_files

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "a.docx").write_bytes(b"PK\x03\x04fake")
    (raw_dir / "b.pdf").write_bytes(b"%PDF-1.4 fake")
    (raw_dir / "c.md").write_text("# md", encoding="utf-8")
    (raw_dir / "d.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (raw_dir / "e.xyz").write_bytes(b"unsupported")  # 应被忽略

    kb = _make_fake_kb("general")
    files = _scan_raw_files(kb, raw_dir)
    names = [fp.name for fp in files]
    assert "a.docx" in names
    assert "b.pdf" in names
    assert "c.md" in names
    assert "d.png" in names
    assert "e.xyz" not in names


def test_scan_raw_files_empty_dir(tmp_path: Path):
    from custom_app.api.kb import _scan_raw_files

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    kb = _make_fake_kb("sop_docx")
    assert _scan_raw_files(kb, raw_dir) == []


def test_scan_raw_files_missing_dir(tmp_path: Path):
    from custom_app.api.kb import _scan_raw_files

    kb = _make_fake_kb("general")
    # 不存在的目录应安全返回空，不抛
    assert _scan_raw_files(kb, tmp_path / "does_not_exist") == []


def test_kb_type_defaults_to_sop_when_missing(tmp_path: Path):
    """老 KB 字典无 type 字段时应退化到 sop_docx。"""
    from custom_app.api.kb import _kb_type

    assert _kb_type({"kb_id": "old"}) == "sop_docx"
    assert _kb_type({"kb_id": "old", "type": None}) == "sop_docx"
    assert _kb_type({"kb_id": "old", "type": ""}) == "sop_docx"
    assert _kb_type({"kb_id": "new", "type": "general"}) == "general"


# ---------------------------------------------------------------------------
# _parse_stage 路由
# ---------------------------------------------------------------------------


def test_parse_stage_sop_uses_parse_directory(tmp_path: Path):
    """sop_docx KB 必须走 docx_parser.parse_directory 路径，保留业务定制分块。"""
    from custom_app.api import kb as kb_mod

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "doc.docx").write_bytes(b"PK\x03\x04fake")
    chunks_path = tmp_path / "chunks.jsonl"

    kb = _make_fake_kb("sop_docx")
    fake_chunks = [{"id": "doc_step_1", "title": "STEP 1", "contents": "x", "doc": "doc"}]

    with patch.object(kb_mod, "parse_directory", return_value=fake_chunks) as m_parse, \
         patch.object(kb_mod, "write_chunks_jsonl") as m_write:
        kb_mod._parse_stage(kb, raw_dir, tmp_path, chunks_path)

    m_parse.assert_called_once_with(raw_dir, tmp_path)
    m_write.assert_called_once_with(fake_chunks, chunks_path)


def test_parse_stage_general_uses_factory(tmp_path: Path):
    """general KB 必须走 parsers.parse_files 工厂，每文件分发。"""
    from custom_app.api import kb as kb_mod
    from custom_app.services.parsers.schema import Chunk, ChunkStructure

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "a.md").write_text("# A\nbody\n", encoding="utf-8")
    chunks_path = tmp_path / "chunks.jsonl"

    kb = _make_fake_kb("general", kb_id="general_kb")

    # parsers.parse_files 是真实调用（MarkdownParser 无重型依赖）
    kb_mod._parse_stage(kb, raw_dir, tmp_path, chunks_path)

    # chunks.jsonl 应被写入，且 parser 字段为 markdown
    assert chunks_path.exists()
    lines = chunks_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 1
    first = json.loads(lines[0])
    assert first["parser"] == "markdown"
    assert first["source_type"] == "markdown"
    # kb_id 应被工厂注入
    assert first["kb_id"] == "general_kb"


def test_parse_stage_no_files_is_noop(tmp_path: Path):
    """raw_dir 空时 _parse_stage 应安全返回（chunks_path 不写）。"""
    from custom_app.api import kb as kb_mod

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    chunks_path = tmp_path / "chunks.jsonl"
    kb = _make_fake_kb("general")
    kb_mod._parse_stage(kb, raw_dir, tmp_path, chunks_path)
    assert not chunks_path.exists()
