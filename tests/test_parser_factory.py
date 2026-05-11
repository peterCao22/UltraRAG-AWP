"""Phase 4.1 — Parser 工厂路由测试。

不实际加载 MinerU/Docling 模型；通过 mock import 验证路由表正确性。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from custom_app.services.parsers.factory import (
    KB_TYPE_GENERAL,
    KB_TYPE_SOP_DOCX,
    ParserNotAvailableError,
    get_parser,
    get_supported_extensions,
    is_supported,
)


# ---------------------------------------------------------------------------
# is_supported / get_supported_extensions
# ---------------------------------------------------------------------------


def test_supported_extensions_sop_docx():
    exts = get_supported_extensions(KB_TYPE_SOP_DOCX)
    assert exts == {".docx"}


def test_supported_extensions_general():
    exts = get_supported_extensions(KB_TYPE_GENERAL)
    # 应该包含 docx + pdf + 5 种图片 + markdown 系列 + txt
    assert ".docx" in exts
    assert ".pdf" in exts
    assert ".png" in exts
    assert ".jpg" in exts
    assert ".md" in exts
    assert ".txt" in exts
    # 至少 10 种
    assert len(exts) >= 10


def test_supported_extensions_invalid_kb_type():
    with pytest.raises(ValueError, match="invalid kb_type"):
        get_supported_extensions("nope")


@pytest.mark.parametrize(
    "kb_type,filename,expected",
    [
        (KB_TYPE_SOP_DOCX, "doc.docx", True),
        (KB_TYPE_SOP_DOCX, "doc.pdf", False),  # SOP KB 不接受 PDF
        (KB_TYPE_SOP_DOCX, "doc.md", False),
        (KB_TYPE_GENERAL, "doc.pdf", True),
        (KB_TYPE_GENERAL, "doc.docx", True),
        (KB_TYPE_GENERAL, "doc.png", True),
        (KB_TYPE_GENERAL, "doc.jpg", True),
        (KB_TYPE_GENERAL, "doc.md", True),
        (KB_TYPE_GENERAL, "doc.txt", True),
        (KB_TYPE_GENERAL, "doc.xyz", False),  # 未注册扩展名
        ("invalid_kb", "doc.docx", False),
    ],
)
def test_is_supported(kb_type: str, filename: str, expected: bool):
    assert is_supported(kb_type, Path(filename)) is expected


def test_is_supported_case_insensitive_ext():
    """扩展名大小写不敏感（.DOCX 应当被识别）。"""
    assert is_supported(KB_TYPE_SOP_DOCX, Path("doc.DOCX")) is True
    assert is_supported(KB_TYPE_GENERAL, Path("doc.PDF")) is True


# ---------------------------------------------------------------------------
# get_parser 路由
# ---------------------------------------------------------------------------


def test_get_parser_invalid_kb_type():
    with pytest.raises(ValueError, match="invalid kb_type"):
        get_parser("nope", Path("doc.docx"))


def test_get_parser_unsupported_ext():
    with pytest.raises(ParserNotAvailableError, match="no parser registered"):
        get_parser(KB_TYPE_GENERAL, Path("doc.xyz"))


def test_get_parser_sop_docx_returns_adapter():
    """sop_docx + .docx → DocxParserAdapter（运行时实例化，不依赖 faiss/torch）。"""
    p = get_parser(KB_TYPE_SOP_DOCX, Path("file.docx"))
    from custom_app.services.parsers.factory import _DocxParserAdapter

    assert isinstance(p, _DocxParserAdapter)


def test_get_parser_general_docx_returns_docling():
    """general + .docx → DoclingParser；mock docling 模块避免真实依赖。"""
    fake_docling = type(sys)("custom_app.services.parsers.docling_parser")

    class FakeDoclingParser:
        def parse(self, fp, kb_root):
            return []

    fake_docling.DoclingParser = FakeDoclingParser  # type: ignore[attr-defined]
    with patch.dict(
        sys.modules,
        {"custom_app.services.parsers.docling_parser": fake_docling},
    ):
        p = get_parser(KB_TYPE_GENERAL, Path("file.docx"))
        assert isinstance(p, FakeDoclingParser)


def test_get_parser_pdf_returns_mineru():
    fake_mineru = type(sys)("custom_app.services.parsers.mineru_parser")

    class FakeMineruParser:
        def parse(self, fp, kb_root):
            return []

    fake_mineru.MineruParser = FakeMineruParser  # type: ignore[attr-defined]
    with patch.dict(
        sys.modules,
        {"custom_app.services.parsers.mineru_parser": fake_mineru},
    ):
        p = get_parser(KB_TYPE_GENERAL, Path("file.pdf"))
        assert isinstance(p, FakeMineruParser)


def test_get_parser_image_returns_mineru():
    fake_mineru = type(sys)("custom_app.services.parsers.mineru_parser")

    class FakeMineruParser:
        def parse(self, fp, kb_root):
            return []

    fake_mineru.MineruParser = FakeMineruParser  # type: ignore[attr-defined]
    with patch.dict(
        sys.modules,
        {"custom_app.services.parsers.mineru_parser": fake_mineru},
    ):
        for ext in [".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif"]:
            p = get_parser(KB_TYPE_GENERAL, Path(f"file{ext}"))
            assert isinstance(p, FakeMineruParser), f"failed for ext={ext}"


def test_get_parser_md_returns_markdown_parser():
    from custom_app.services.parsers.markdown_parser import MarkdownParser

    for ext in [".md", ".markdown", ".txt"]:
        p = get_parser(KB_TYPE_GENERAL, Path(f"file{ext}"))
        assert isinstance(p, MarkdownParser), f"failed for ext={ext}"


def test_get_parser_raises_when_mineru_unavailable():
    """模拟 mineru_parser 模块 import 失败时，应抛 ParserNotAvailableError。"""
    # 通过 patch.dict 让 import 时抛 ImportError
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "custom_app.services.parsers.mineru_parser":
            raise ImportError("mineru CLI not installed")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        with pytest.raises(ParserNotAvailableError, match="MineruParser unavailable"):
            get_parser(KB_TYPE_GENERAL, Path("file.pdf"))


# ---------------------------------------------------------------------------
# DocxParserAdapter 真实跑通
# ---------------------------------------------------------------------------


def test_docx_adapter_real_run():
    """DocxParserAdapter 应能跑通真实 SOP docx 文件。"""
    raw_dir = Path("data/kb/agv_demo/raw")
    if not raw_dir.exists():
        pytest.skip(f"sample dir missing: {raw_dir}")
    docx_files = list(raw_dir.glob("*.docx"))
    if not docx_files:
        pytest.skip("no sample docx found")

    p = get_parser(KB_TYPE_SOP_DOCX, docx_files[0])
    chunks = p.parse(docx_files[0], Path("data/kb/agv_demo"))
    assert len(chunks) > 0
    from custom_app.services.parsers.schema import Chunk

    for c in chunks:
        assert isinstance(c, Chunk)
        assert c.source_type == "sop_docx"
        assert c.parser == "docx_parser"
