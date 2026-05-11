"""Phase 4.1 — DoclingParser 单元测试。

通过 mock docling 模块验证：
    - 缺 docling 包时报错
    - 非 .docx 扩展名报错
    - 委托给 MarkdownParser 切块，输出 source_type=general_docx / parser=docling

集成测试（需真实 docling 安装）请用 -m requires_docling 触发。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_app.services.parsers.docling_parser import DoclingParser


def test_missing_file_raises(tmp_path: Path):
    p = DoclingParser()
    with pytest.raises(FileNotFoundError):
        p.parse(tmp_path / "missing.docx", tmp_path)


def test_non_docx_raises(tmp_path: Path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    p = DoclingParser()
    with pytest.raises(ValueError, match="only supports .docx"):
        p.parse(f, tmp_path)


def test_docling_not_installed_raises(tmp_path: Path):
    f = tmp_path / "doc.docx"
    f.write_bytes(b"PK\x03\x04fake-zip")  # docx 是 zip 容器

    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "docling.document_converter" or name.startswith("docling."):
            raise ImportError("No module named 'docling'")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        p = DoclingParser()
        with pytest.raises(RuntimeError, match="docling not installed"):
            p.parse(f, tmp_path)


def _make_fake_docling_module(markdown_output: str):
    """构造 mock docling.document_converter 模块。"""
    fake_doc = SimpleNamespace(
        export_to_markdown=MagicMock(return_value=markdown_output)
    )
    fake_result = SimpleNamespace(document=fake_doc)
    fake_converter_cls = MagicMock()
    fake_converter_cls.return_value.convert = MagicMock(return_value=fake_result)
    fake_module = SimpleNamespace(DocumentConverter=fake_converter_cls)
    return fake_module, fake_converter_cls


def test_basic_parse_with_mock_docling(tmp_path: Path):
    """mock docling 返回 markdown，DoclingParser 应委托 MarkdownParser 切块。"""
    f = tmp_path / "manual.docx"
    f.write_bytes(b"PK\x03\x04fake-zip")
    md = "# 章节1\n第一章正文。\n\n# 章节2\n第二章正文。\n"
    fake_module, _ = _make_fake_docling_module(md)
    # docling.document_converter 是嵌套 import，需要 patch 上级
    fake_docling = SimpleNamespace(document_converter=fake_module)
    with patch.dict(
        sys.modules,
        {
            "docling": fake_docling,
            "docling.document_converter": fake_module,
        },
    ):
        p = DoclingParser()
        chunks = p.parse(f, tmp_path)

    assert len(chunks) == 2
    assert chunks[0].title == "章节1"
    assert chunks[0].structure.heading_path == ("章节1",)
    assert chunks[0].source_type == "general_docx"  # 被重打标
    assert chunks[0].parser == "docling"
    assert chunks[1].title == "章节2"


def test_cache_dir_cleaned_up(tmp_path: Path):
    """临时 markdown 缓存应该在 parse 完成后被清理。"""
    f = tmp_path / "tidy.docx"
    f.write_bytes(b"PK\x03\x04fake-zip")
    fake_module, _ = _make_fake_docling_module("# 标题\n正文\n")
    fake_docling = SimpleNamespace(document_converter=fake_module)
    with patch.dict(
        sys.modules,
        {
            "docling": fake_docling,
            "docling.document_converter": fake_module,
        },
    ):
        p = DoclingParser()
        p.parse(f, tmp_path)
    # .docling_cache 目录可能保留（空目录），但临时 .md 文件应被清理
    cache_dir = tmp_path / ".docling_cache"
    if cache_dir.exists():
        leftover_md = list(cache_dir.glob("*.md"))
        assert leftover_md == [], f"leaked markdown cache: {leftover_md}"


def test_convert_failure_raises(tmp_path: Path):
    """docling convert 抛异常时应被包装为 RuntimeError。"""
    f = tmp_path / "bad.docx"
    f.write_bytes(b"PK\x03\x04corrupt")
    fake_converter_cls = MagicMock()
    fake_converter_cls.return_value.convert = MagicMock(
        side_effect=Exception("internal docling error")
    )
    fake_module = SimpleNamespace(DocumentConverter=fake_converter_cls)
    fake_docling = SimpleNamespace(document_converter=fake_module)
    with patch.dict(
        sys.modules,
        {
            "docling": fake_docling,
            "docling.document_converter": fake_module,
        },
    ):
        p = DoclingParser()
        with pytest.raises(RuntimeError, match="docling convert failed"):
            p.parse(f, tmp_path)


def test_no_document_in_result_raises(tmp_path: Path):
    f = tmp_path / "noop.docx"
    f.write_bytes(b"PK\x03\x04fake")
    fake_result = SimpleNamespace(document=None)
    fake_converter_cls = MagicMock()
    fake_converter_cls.return_value.convert = MagicMock(return_value=fake_result)
    fake_module = SimpleNamespace(DocumentConverter=fake_converter_cls)
    fake_docling = SimpleNamespace(document_converter=fake_module)
    with patch.dict(
        sys.modules,
        {
            "docling": fake_docling,
            "docling.document_converter": fake_module,
        },
    ):
        p = DoclingParser()
        with pytest.raises(RuntimeError, match="no document"):
            p.parse(f, tmp_path)


def test_protocol_compliance():
    from custom_app.services.parsers.base import Parser

    p = DoclingParser()
    assert isinstance(p, Parser)


# ---------------------------------------------------------------------------
# 集成测试（需真实 docling；CI 默认 skip）
# ---------------------------------------------------------------------------


@pytest.mark.requires_docling
def test_integration_real_docling_docx(tmp_path: Path):
    """需要环境装好 docling；运行：pytest -m requires_docling"""
    try:
        from docling.document_converter import DocumentConverter  # noqa: F401
    except ImportError:
        pytest.skip("docling not installed")

    sample = Path("data/kb/ifs_docs/raw")
    if not sample.exists():
        pytest.skip(f"sample dir missing: {sample}")
    docx_files = list(sample.glob("*.docx"))
    if not docx_files:
        pytest.skip("no docx sample found")

    p = DoclingParser()
    chunks = p.parse(docx_files[0], tmp_path)
    assert len(chunks) > 0
    for c in chunks:
        assert c.source_type == "general_docx"
        assert c.parser == "docling"
