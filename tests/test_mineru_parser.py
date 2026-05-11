"""Phase 4.1 — MineruParser 单元测试。

不依赖真实 MinerU CLI / 模型。通过 mock subprocess.run 写入预制 content_list.json，
验证：
    - 缺 mineru 可执行时报错
    - subprocess 失败时报错
    - 在 content_list 上正确切块（按 text_level）
    - 图片搬迁到 kb_root/images/<doc_stem>/
    - 表格 / 公式被收集

集成测试（需真实 MinerU 模型）请见 manual fixture。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from custom_app.services.parsers.mineru_parser import MineruParser
from custom_app.services.parsers.schema import Chunk


# ---------------------------------------------------------------------------
# 基础行为
# ---------------------------------------------------------------------------


def test_missing_file_raises(tmp_path: Path):
    p = MineruParser()
    with pytest.raises(FileNotFoundError):
        p.parse(tmp_path / "missing.pdf", tmp_path)


def test_unsupported_ext_raises(tmp_path: Path):
    p = MineruParser()
    f = tmp_path / "doc.docx"
    f.write_bytes(b"fake")
    with pytest.raises(ValueError, match="unsupported"):
        p.parse(f, tmp_path)


def test_missing_executable_raises(tmp_path: Path):
    """mineru 不在 PATH 时应给出友好错误。"""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    p = MineruParser(mineru_executable="this_does_not_exist_xyz")
    with pytest.raises(RuntimeError, match="mineru executable not found"):
        p.parse(f, tmp_path)


# ---------------------------------------------------------------------------
# Mock CLI：注入预制 content_list.json
# ---------------------------------------------------------------------------


def _make_fake_subprocess_run(content_list: list, images_to_create: dict[str, bytes] | None = None):
    """生成 mock subprocess.run 的工厂；调用时把 content_list 和图片落到输出目录。

    根据 MineruParser._locate_outputs 期望的扁平结构：
        <output_dir>/<stem>_content_list.json
        <output_dir>/images/*.png
    """

    def fake_run(cmd, **kwargs):
        # 解析 -p <input> -o <output>
        args = list(cmd)
        input_path = Path(args[args.index("-p") + 1])
        output_dir = Path(args[args.index("-o") + 1])
        stem = input_path.stem
        # 写 content_list.json
        json_path = output_dir / f"{stem}_content_list.json"
        json_path.write_text(json.dumps(content_list, ensure_ascii=False), encoding="utf-8")
        # 写图片
        if images_to_create:
            img_dir = output_dir / "images"
            img_dir.mkdir(parents=True, exist_ok=True)
            for name, data in images_to_create.items():
                (img_dir / name).write_bytes(data)
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    return fake_run


@patch("custom_app.services.parsers.mineru_parser.shutil.which", return_value="/fake/mineru")
def test_cli_failure_raises(_which, tmp_path: Path):
    """subprocess.run 返回非 0 时应抛 RuntimeError。"""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake")

    def fail_run(cmd, **kwargs):
        r = MagicMock()
        r.returncode = 1
        r.stdout = ""
        r.stderr = "internal error: model not loaded"
        return r

    with patch("custom_app.services.parsers.mineru_parser.subprocess.run", side_effect=fail_run):
        p = MineruParser()
        with pytest.raises(RuntimeError, match="mineru CLI failed"):
            p.parse(f, tmp_path)


@patch("custom_app.services.parsers.mineru_parser.shutil.which", return_value="/fake/mineru")
def test_missing_output_raises(_which, tmp_path: Path):
    """mineru exit 0 但没有 content_list.json 应抛 RuntimeError。"""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake")

    def empty_run(cmd, **kwargs):
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        return r  # 不写任何输出文件

    with patch("custom_app.services.parsers.mineru_parser.subprocess.run", side_effect=empty_run):
        p = MineruParser()
        with pytest.raises(RuntimeError, match="mineru output not found"):
            p.parse(f, tmp_path)


@patch("custom_app.services.parsers.mineru_parser.shutil.which", return_value="/fake/mineru")
def test_basic_heading_split(_which, tmp_path: Path):
    """按 text_level 切块，重建 heading_path 层级链。"""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    content = [
        {"type": "text", "text": "第3章 故障处理", "text_level": 1, "page_idx": 0},
        {"type": "text", "text": "章节引言。", "text_level": 0, "page_idx": 0},
        {"type": "text", "text": "3.1 电池告警", "text_level": 2, "page_idx": 1},
        {"type": "text", "text": "电池告警正文。", "text_level": 0, "page_idx": 1},
        {"type": "text", "text": "3.2 通信告警", "text_level": 2, "page_idx": 2},
        {"type": "text", "text": "通信告警正文。", "text_level": 0, "page_idx": 2},
    ]
    with patch(
        "custom_app.services.parsers.mineru_parser.subprocess.run",
        side_effect=_make_fake_subprocess_run(content),
    ):
        p = MineruParser()
        chunks = p.parse(f, tmp_path)

    assert len(chunks) == 3
    assert chunks[0].title == "第3章 故障处理"
    assert chunks[0].structure.heading_path == ("第3章 故障处理",)
    assert chunks[0].structure.heading_level == 1
    assert "章节引言" in chunks[0].contents

    assert chunks[1].title == "3.1 电池告警"
    assert chunks[1].structure.heading_path == ("第3章 故障处理", "3.1 电池告警")
    assert chunks[1].structure.heading_level == 2

    # H2 之间深层级清空，但 H1 保留
    assert chunks[2].structure.heading_path == ("第3章 故障处理", "3.2 通信告警")


@patch("custom_app.services.parsers.mineru_parser.shutil.which", return_value="/fake/mineru")
def test_image_relocation(_which, tmp_path: Path):
    """MinerU 图片应被搬到 kb_root/images/<doc_stem>/ 并重命名。"""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    content = [
        {"type": "text", "text": "标题", "text_level": 1, "page_idx": 0},
        {
            "type": "image",
            "img_path": "images/orig.png",
            "image_caption": ["电池图"],
            "page_idx": 0,
        },
        {"type": "text", "text": "正文", "text_level": 0, "page_idx": 0},
    ]
    fake_run = _make_fake_subprocess_run(
        content,
        images_to_create={"orig.png": b"\x89PNG\r\n\x1a\nfake"},
    )
    with patch("custom_app.services.parsers.mineru_parser.subprocess.run", side_effect=fake_run):
        p = MineruParser()
        chunks = p.parse(f, tmp_path)

    assert len(chunks) == 1
    c = chunks[0]
    assert len(c.images) == 1
    img = c.images[0]
    # 应该被映射到 kb_root/images/doc/img_0001.png
    assert img.path == "images/doc/img_0001.png"
    assert img.caption == "电池图"
    assert img.img_id == "doc_img_0001"
    # 图片实际落地
    expected = tmp_path / "images" / "doc" / "img_0001.png"
    assert expected.exists()
    # 正文内有 [IMG: ...] 占位
    assert "[IMG: images/doc/img_0001.png]" in c.contents


@patch("custom_app.services.parsers.mineru_parser.shutil.which", return_value="/fake/mineru")
def test_table_and_equation_collected(_which, tmp_path: Path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    content = [
        {"type": "text", "text": "数据", "text_level": 1, "page_idx": 0},
        {
            "type": "table",
            "table_body": "| A | B |\n|---|---|\n| 1 | 2 |",
            "table_caption": ["表1: 对比"],
            "page_idx": 0,
        },
        {
            "type": "equation",
            "latex": "E = mc^2",
            "page_idx": 0,
        },
    ]
    with patch(
        "custom_app.services.parsers.mineru_parser.subprocess.run",
        side_effect=_make_fake_subprocess_run(content),
    ):
        p = MineruParser()
        chunks = p.parse(f, tmp_path)

    assert len(chunks) == 1
    c = chunks[0]
    assert len(c.tables) == 1
    assert c.tables[0].markdown.startswith("| A | B |")
    assert c.tables[0].caption == "表1: 对比"
    # 公式以 $$ 包裹放进 contents
    assert "E = mc^2" in c.contents
    assert "$$" in c.contents
    # 表格 markdown 也在 contents
    assert "| A | B |" in c.contents


@patch("custom_app.services.parsers.mineru_parser.shutil.which", return_value="/fake/mineru")
def test_no_headings_fallback_single_chunk(_which, tmp_path: Path):
    """整文档无标题：fallback 单 chunk。"""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    content = [
        {"type": "text", "text": "纯正文段落1", "text_level": 0, "page_idx": 0},
        {"type": "text", "text": "纯正文段落2", "text_level": 0, "page_idx": 0},
    ]
    with patch(
        "custom_app.services.parsers.mineru_parser.subprocess.run",
        side_effect=_make_fake_subprocess_run(content),
    ):
        p = MineruParser()
        chunks = p.parse(f, tmp_path)
    # 应有内容生成（不要求一定是单 chunk，但至少有 1 个）
    assert len(chunks) >= 1
    combined = "\n".join(c.contents for c in chunks)
    assert "纯正文段落1" in combined
    assert "纯正文段落2" in combined


@patch("custom_app.services.parsers.mineru_parser.shutil.which", return_value="/fake/mineru")
def test_image_source_type(_which, tmp_path: Path):
    """图片文件（非 PDF）source_type 应为 'image'。"""
    f = tmp_path / "scan.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    content = [{"type": "text", "text": "扫描件文字", "text_level": 0}]
    with patch(
        "custom_app.services.parsers.mineru_parser.subprocess.run",
        side_effect=_make_fake_subprocess_run(content),
    ):
        p = MineruParser()
        chunks = p.parse(f, tmp_path)
    assert chunks[0].source_type == "image"


@patch("custom_app.services.parsers.mineru_parser.shutil.which", return_value="/fake/mineru")
def test_chunk_serialization(_which, tmp_path: Path):
    """MineruParser 输出能正确序列化到 chunks.jsonl 格式。"""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    content = [
        {"type": "text", "text": "标题", "text_level": 1, "page_idx": 0},
        {"type": "text", "text": "正文", "text_level": 0, "page_idx": 0},
    ]
    with patch(
        "custom_app.services.parsers.mineru_parser.subprocess.run",
        side_effect=_make_fake_subprocess_run(content),
    ):
        p = MineruParser()
        chunks = p.parse(f, tmp_path)
    d = chunks[0].to_jsonl_dict()
    assert d["parser"] == "mineru"
    assert d["source_type"] == "general_pdf"
    assert d["structure"]["heading_path"] == ["标题"]
    assert d["structure"]["heading_level"] == 1


def test_protocol_compliance():
    from custom_app.services.parsers.base import Parser

    p = MineruParser()
    assert isinstance(p, Parser)


# ---------------------------------------------------------------------------
# 集成测试（需真实 MinerU；CI 默认 skip）
# ---------------------------------------------------------------------------


@pytest.mark.requires_mineru
def test_integration_real_mineru_pdf(tmp_path: Path):
    """需要环境装好 mineru + 模型；运行：pytest -m requires_mineru"""
    import shutil as _shutil

    if _shutil.which("mineru") is None:
        pytest.skip("mineru CLI not installed")
    # 准备一个最小 PDF（用户可在 data/kb/ 下放样本）
    sample_pdf = Path("data/kb/general/raw/sample.pdf")
    if not sample_pdf.exists():
        pytest.skip(f"sample missing: {sample_pdf}")
    p = MineruParser()
    chunks = p.parse(sample_pdf, tmp_path)
    assert len(chunks) > 0
    for c in chunks:
        assert isinstance(c, Chunk)
        assert c.source_type == "general_pdf"
