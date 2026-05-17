"""Phase 8.0 —— 兜底滑窗切分单元测试。

覆盖：
1. 短文档（< 阈值）→ 单 _intro chunk，向后兼容（命名沿用，不改成 _full）
2. 长文档（≥ 阈值）→ 多 _window_N chunk
3. overlap 段落只复制文本、不复制图片（同图不出现在相邻 chunk）
4. 表格作为整段加入 buffer，不被切断
5. 单段超过 size 时整段保留，不切碎
6. 集成：构造一份长 FAQ docx，验证 parse_docx 切出 _window_N

> PLAN §四.3 原方案使用 `_full` 命名长文档兜底前的短文档分支；为满足 §九
> "现有 agv_demo / ifs_docs chunks.jsonl 不变" 的兼容性验收，本期保留 `_intro`
> 命名，仅长文档新增 `_window_N` 命名。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

from custom_app.services.docx_parser import (
    SLIDING_WINDOW_OVERLAP_CHARS,
    SLIDING_WINDOW_SIZE_CHARS,
    SLIDING_WINDOW_THRESHOLD_CHARS,
    _sliding_window_chunks,
    parse_docx,
)


# ─────────────────────────────────────────────────────────────────────────────
# 纯函数：_sliding_window_chunks
# ─────────────────────────────────────────────────────────────────────────────


class TestSlidingWindowPureFunction:
    """对 _sliding_window_chunks 的行为做最小化验证。"""

    def test_single_short_part_returns_one_window(self) -> None:
        """单段短文本 → 1 个 window。"""
        parts = ["这是一段短文本"]
        imgs: List[List[str]] = [[]]
        out = _sliding_window_chunks(parts, imgs, size=800, overlap=100)
        assert len(out) == 1
        assert out[0][0] == ["这是一段短文本"]
        assert out[0][1] == []

    def test_long_sequence_splits_into_multiple_windows(self) -> None:
        """多段总长远超 size → 拆成多个 window。"""
        # 6 段各 300 字 = 1800 字，size=800 → 至少 3 个 window
        parts = ["甲" * 300, "乙" * 300, "丙" * 300, "丁" * 300, "戊" * 300, "己" * 300]
        imgs: List[List[str]] = [[] for _ in parts]
        out = _sliding_window_chunks(parts, imgs, size=800, overlap=100)
        assert len(out) >= 3, f"expected ≥3 windows, got {len(out)}"
        # 每个 window 至少包含一段
        for lines, _ in out:
            assert lines

    def test_overlap_copies_text_but_not_images(self) -> None:
        """overlap 段落带图：图片只归原 chunk，不复制到下一 chunk。"""
        # 段 A=500 字 + 段 B=500 字（带 1 张图）+ 段 C=500 字（带 1 张图）
        # 累积顺序：A(500) → 加 B 后 1000>800 触发 flush，buf=[A,B], imgs=[imgB]
        # overlap=100，B(500) > 100 → tail 仅留 B；imgs 清空
        # 再加 C：buf=[B,C], imgs=[imgC]，B 的图 imgB 不在第二个 window
        parts = ["甲" * 500, "乙" * 500, "丙" * 500]
        imgs = [[], ["images/doc/imgB.png"], ["images/doc/imgC.png"]]
        out = _sliding_window_chunks(parts, imgs, size=800, overlap=100)
        assert len(out) >= 2

        all_imgs_per_window = [w[1] for w in out]
        flat = [p for w in all_imgs_per_window for p in w]
        # 每张图最多出现一次（imgB 和 imgC 各 1 次）
        assert flat.count("images/doc/imgB.png") == 1
        assert flat.count("images/doc/imgC.png") == 1

    def test_oversized_single_part_kept_whole(self) -> None:
        """单段超过 size：整段保留，不切碎。"""
        big = "庞" * 2000  # 单段 2000 字，size=800
        parts = [big, "尾段 100 字" + "余" * 90]
        imgs: List[List[str]] = [[], []]
        out = _sliding_window_chunks(parts, imgs, size=800, overlap=100)
        # 第一个 window 应完整包含 big
        assert big in out[0][0]
        # 总段落数（去重）等于输入段落数
        seen_big = sum(1 for w_lines, _ in out for ln in w_lines if ln == big)
        assert seen_big == 1, "超大段不应被切碎或复制"

    def test_misaligned_inputs_raise(self) -> None:
        """parts 与 imgs_per_part 长度不一致 → ValueError。"""
        with pytest.raises(ValueError):
            _sliding_window_chunks(["a", "b"], [[]], size=800, overlap=100)

    def test_table_like_long_line_is_one_part(self) -> None:
        """表格被压成单行长字符串（_table_to_text 行为）—— 应作为整段加入 buffer。"""
        # 模拟：3 段普通文本 + 1 个超长表格行 + 2 段普通文本
        table_line = "列1 | 列2 | 列3\n" + "数据行" * 100  # ~600+ 字符
        parts = ["普通段 1" * 50, "普通段 2" * 50, table_line, "尾段 1" * 50, "尾段 2" * 50]
        imgs: List[List[str]] = [[] for _ in parts]
        out = _sliding_window_chunks(parts, imgs, size=800, overlap=100)
        # 表格整行必须出现在某一个 window 中（不被切断）
        appearances = sum(1 for w_lines, _ in out for ln in w_lines if ln == table_line)
        assert appearances >= 1


# ─────────────────────────────────────────────────────────────────────────────
# 集成：parse_docx 走兜底路径
# ─────────────────────────────────────────────────────────────────────────────


def _make_long_faq_docx(tmp_path: Path, *, total_chars: int = 1500) -> Path:
    """构造一份既无 STEP 也无 Heading 的长文档（走 parse_docx 兜底）。"""
    from docx import Document  # type: ignore

    doc = Document()
    # 用 N 段 ~ 200 字普通段落（默认 Normal 样式，不会被 _paragraph_heading_label 识别）
    per_para = 200
    n = max(1, total_chars // per_para)
    for i in range(n):
        doc.add_paragraph(f"问答条目 {i + 1}：" + "内容字符" * (per_para // 4))
    path = tmp_path / "faq_long.docx"
    doc.save(str(path))
    return path


def _make_short_faq_docx(tmp_path: Path) -> Path:
    from docx import Document  # type: ignore

    doc = Document()
    doc.add_paragraph("这是一段非常短的 FAQ。")
    doc.add_paragraph("第二段也很短。")
    path = tmp_path / "faq_short.docx"
    doc.save(str(path))
    return path


class TestParseDocxFallbackRouting:
    """parse_docx 兜底分支按字符阈值路由。"""

    def test_short_document_keeps_single_intro_chunk(self, tmp_path: Path) -> None:
        """< 阈值的短文档保持 _intro 单 chunk（向后兼容现有 KB）。"""
        kb_root = tmp_path / "kb"
        kb_root.mkdir()
        docx = _make_short_faq_docx(tmp_path)
        chunks = parse_docx(docx, kb_root)
        assert len(chunks) == 1
        assert chunks[0]["id"].endswith("_intro")
        # 不应触发滑窗
        assert "_window_" not in chunks[0]["id"]

    def test_long_document_routed_to_sliding_windows(self, tmp_path: Path) -> None:
        """≥ 阈值的长文档切出多个 _window_N chunk。"""
        kb_root = tmp_path / "kb"
        kb_root.mkdir()
        # 1500 字明显 > 阈值 800，应至少切 2 块
        docx = _make_long_faq_docx(tmp_path, total_chars=1500)
        chunks = parse_docx(docx, kb_root)

        window_ids = [c["id"] for c in chunks if "_window_" in c["id"]]
        intro_ids = [c["id"] for c in chunks if c["id"].endswith("_intro")]

        assert len(window_ids) >= 2, f"expected ≥2 _window_N chunks, got: {[c['id'] for c in chunks]}"
        assert not intro_ids, "long document should not collapse to a single _intro chunk"

        # schema 字段保留（新 schema 兼容）
        for c in chunks:
            assert c["source_type"] == "sop_docx"
            assert c["parser"] == "docx_parser"
            assert isinstance(c["images"], list)

    def test_threshold_constants_are_sane(self) -> None:
        """阈值常量自洽：threshold/size 至少 ≥ overlap*2。"""
        assert SLIDING_WINDOW_THRESHOLD_CHARS >= SLIDING_WINDOW_OVERLAP_CHARS * 2
        assert SLIDING_WINDOW_SIZE_CHARS >= SLIDING_WINDOW_OVERLAP_CHARS * 2
