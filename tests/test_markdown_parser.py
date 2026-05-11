"""Phase 4.1 — MarkdownParser 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_app.services.parsers.markdown_parser import MarkdownParser


@pytest.fixture
def parser() -> MarkdownParser:
    return MarkdownParser()


def test_unsupported_extension(parser: MarkdownParser, tmp_path: Path):
    f = tmp_path / "x.pdf"
    f.write_text("hello")
    with pytest.raises(ValueError, match="unsupported"):
        parser.parse(f, tmp_path)


def test_missing_file(parser: MarkdownParser, tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        parser.parse(tmp_path / "missing.md", tmp_path)


def test_empty_file_returns_empty(parser: MarkdownParser, tmp_path: Path):
    f = tmp_path / "empty.md"
    f.write_text("")
    assert parser.parse(f, tmp_path) == []


def test_txt_file_single_chunk(parser: MarkdownParser, tmp_path: Path):
    """.txt 文件不解析标题，整文档作为一个 chunk。"""
    f = tmp_path / "note.txt"
    f.write_text("# 这看起来像标题但是 txt 文件\n正文 1\n正文 2\n", encoding="utf-8")
    chunks = parser.parse(f, tmp_path)
    assert len(chunks) == 1
    c = chunks[0]
    assert c.source_type == "markdown"
    assert c.parser == "markdown"
    assert "# 这看起来像标题" in c.contents
    assert c.structure.heading_path == ()
    assert c.structure.heading_level == 0


def test_md_no_headings_single_chunk(parser: MarkdownParser, tmp_path: Path):
    """没有任何标题的 markdown：fallback 单 chunk。"""
    f = tmp_path / "plain.md"
    f.write_text("纯文本 markdown，没有标题。\n第二行。\n", encoding="utf-8")
    chunks = parser.parse(f, tmp_path)
    assert len(chunks) == 1
    assert chunks[0].structure.heading_path == ()


def test_md_h1_only(parser: MarkdownParser, tmp_path: Path):
    f = tmp_path / "doc.md"
    f.write_text(
        "# 章节A\n这是 A 的正文。\n\n# 章节B\n这是 B 的正文。\n",
        encoding="utf-8",
    )
    chunks = parser.parse(f, tmp_path)
    assert len(chunks) == 2
    assert chunks[0].title == "章节A"
    assert chunks[0].structure.heading_path == ("章节A",)
    assert chunks[0].structure.heading_level == 1
    assert "A 的正文" in chunks[0].contents
    assert chunks[1].structure.heading_path == ("章节B",)


def test_md_nested_headings_path(parser: MarkdownParser, tmp_path: Path):
    """嵌套标题：heading_path 应包含完整层级链。"""
    f = tmp_path / "nested.md"
    f.write_text(
        "# 第3章 故障处理\n章节引言。\n\n"
        "## 3.1 电池告警\n电池告警正文。\n\n"
        "### 3.1.1 低电量\n低电量处理步骤。\n\n"
        "## 3.2 通信告警\n通信告警正文。\n",
        encoding="utf-8",
    )
    chunks = parser.parse(f, tmp_path)
    assert len(chunks) == 4
    # 第1块：H1 下的引言
    assert chunks[0].structure.heading_path == ("第3章 故障处理",)
    assert chunks[0].structure.heading_level == 1
    # 第2块：H2 电池告警
    assert chunks[1].structure.heading_path == ("第3章 故障处理", "3.1 电池告警")
    assert chunks[1].structure.heading_level == 2
    # 第3块：H3 低电量
    assert chunks[2].structure.heading_path == (
        "第3章 故障处理",
        "3.1 电池告警",
        "3.1.1 低电量",
    )
    assert chunks[2].structure.heading_level == 3
    # 第4块：返回到 H2，深层级清空
    assert chunks[3].structure.heading_path == ("第3章 故障处理", "3.2 通信告警")
    assert chunks[3].structure.heading_level == 2


def test_md_image_extraction(parser: MarkdownParser, tmp_path: Path):
    f = tmp_path / "with_img.md"
    f.write_text(
        "# 图文混排\n"
        "正文 1。\n\n"
        "![电池图](images/battery.png)\n\n"
        '![警示图](images/warn.jpg "警示标题")\n\n'
        "正文 2。\n",
        encoding="utf-8",
    )
    chunks = parser.parse(f, tmp_path)
    assert len(chunks) == 1
    imgs = chunks[0].images
    assert len(imgs) == 2
    assert imgs[0].path == "images/battery.png"
    assert imgs[0].caption == "电池图"
    assert imgs[1].path == "images/warn.jpg"
    assert imgs[1].caption == "警示图"


def test_md_duplicate_images_deduplicated(parser: MarkdownParser, tmp_path: Path):
    f = tmp_path / "dup.md"
    f.write_text(
        "# 标题\n"
        "![a](images/x.png)\n"
        "![b](images/x.png)\n"  # 同路径不同 alt：只取首个
        "![c](images/y.png)\n",
        encoding="utf-8",
    )
    chunks = parser.parse(f, tmp_path)
    assert len(chunks[0].images) == 2
    paths = [img.path for img in chunks[0].images]
    assert paths == ["images/x.png", "images/y.png"]


def test_md_chunk_id_format(parser: MarkdownParser, tmp_path: Path):
    f = tmp_path / "my_doc.md"
    f.write_text("# A\ntext A\n\n# B\ntext B\n", encoding="utf-8")
    chunks = parser.parse(f, tmp_path)
    assert chunks[0].id == "my_doc_section_1"
    assert chunks[1].id == "my_doc_section_2"


def test_md_protocol_compliance(parser: MarkdownParser):
    """MarkdownParser 必须符合 Parser Protocol。"""
    from custom_app.services.parsers.base import Parser

    assert isinstance(parser, Parser)


def test_md_skips_blank_sections(parser: MarkdownParser, tmp_path: Path):
    """连续标题之间没有正文：不应生成空 chunk。"""
    f = tmp_path / "blank.md"
    f.write_text(
        "# A\n\n# B\n正文 B\n",
        encoding="utf-8",
    )
    chunks = parser.parse(f, tmp_path)
    # 只有 B 段有正文
    assert len(chunks) == 1
    assert chunks[0].title == "B"
    assert "正文 B" in chunks[0].contents


def test_serialization_to_jsonl_dict(parser: MarkdownParser, tmp_path: Path):
    """parser 输出能正确序列化到 chunks.jsonl 格式。"""
    f = tmp_path / "serial.md"
    f.write_text("# 测试\n正文\n![img](p.png)\n", encoding="utf-8")
    chunks = parser.parse(f, tmp_path)
    d = chunks[0].to_jsonl_dict()
    assert d["id"] == "serial_section_1"
    assert d["source_type"] == "markdown"
    assert d["parser"] == "markdown"
    assert d["structure"]["heading_path"] == ["测试"]
    assert d["images"][0]["path"] == "p.png"
    assert d["vector_id"] is None
