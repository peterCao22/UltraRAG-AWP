"""Phase 9.2 compose_doc_embedding_text 图片 caption 拼接单元测试。

覆盖：
  1. 无图片 chunk → 行为不变（零回归 Phase 4.3 / 8.2.1）
  2. 旧 schema 字符串 images → 跳过，不污染（向后兼容）
  3. 单图含 caption_zh + caption_en + entities → 附加 image_block
  4. 多图（含失败 + 含部分有效）→ 只拼有 caption 的
  5. 失败图（_describe_failed=True）→ 跳过
  6. salvaged 图（caption_zh 完整、entities 空）→ 仍参与
  7. caption_zh 空但 caption_en 有 → 仍参与
  8. entities 超 8 个 → 截断到 8
  9. images 字段非 list（边界）→ 不报错
"""

from __future__ import annotations

from custom_app.services.google_embedder import (
    _compose_image_block,
    compose_doc_embedding_text,
)


# ---------------------------------------------------------------------------
# 零回归：无图片场景，Phase 4.3 / 8.2.1 行为不变
# ---------------------------------------------------------------------------


def test_no_images_unchanged_basic():
    row = {"title": "T", "contents": "body content"}
    out = compose_doc_embedding_text(row)
    assert out == "T\nbody content"


def test_no_images_with_context_heading():
    row = {
        "context": "doc-level ctx",
        "structure": {"heading_path": ["A", "B"]},
        "title": "T",
        "contents": "body",
    }
    out = compose_doc_embedding_text(row)
    # context > heading > title > body 顺序保持 Phase 8.2.1
    assert out == "doc-level ctx\nA > B\nT\nbody"


def test_empty_images_list_no_block():
    row = {"title": "T", "contents": "x", "images": []}
    out = compose_doc_embedding_text(row)
    assert "[图片]" not in out


# ---------------------------------------------------------------------------
# 向后兼容：旧 schema 字符串 images 跳过
# ---------------------------------------------------------------------------


def test_legacy_string_images_skipped():
    """Phase 9.1 前的 chunks.jsonl 用 images: ['path1', 'path2'] 字符串格式。"""
    row = {
        "title": "T",
        "contents": "body",
        "images": ["images/foo.jpg", "images/bar.png"],
    }
    out = compose_doc_embedding_text(row)
    assert "[图片]" not in out  # 字符串不带 caption，跳过
    assert out == "T\nbody"


def test_non_list_images_does_not_crash():
    """images 字段不是 list（如旧格式或损坏数据）也不报错。"""
    row = {"title": "T", "contents": "x", "images": "single_string"}
    out = compose_doc_embedding_text(row)
    assert out == "T\nx"


# ---------------------------------------------------------------------------
# 主路径：有 caption 的图拼进来
# ---------------------------------------------------------------------------


def test_single_image_with_caption_zh_en_entities():
    row = {
        "title": "Alarm 16 处理",
        "contents": "Master Link Down 处理流程。",
        "images": [{
            "path": "images/x.jpg",
            "caption_zh": "AGV 控制面板显示告警 ID 16 Master Link Down。",
            "caption_en": "AGV control panel showing alarm ID 16 Master Link Down.",
            "entities": ["AGV", "控制面板", "Alarm 16", "Master Link Down"],
        }],
    }
    out = compose_doc_embedding_text(row)
    assert "[图片]" in out
    assert "AGV 控制面板显示告警 ID 16" in out
    assert "AGV control panel" in out
    assert "实体:" in out
    assert "Master Link Down" in out


def test_multiple_images_each_on_own_line():
    row = {
        "title": "T",
        "contents": "x",
        "images": [
            {"path": "a.jpg", "caption_zh": "图 A 描述", "entities": ["A"]},
            {"path": "b.jpg", "caption_zh": "图 B 描述", "entities": ["B"]},
        ],
    }
    out = compose_doc_embedding_text(row)
    # 两张图，应有两行 [图片]
    assert out.count("[图片]") == 2
    assert "图 A 描述" in out
    assert "图 B 描述" in out


# ---------------------------------------------------------------------------
# 失败图 / salvaged / 缺字段
# ---------------------------------------------------------------------------


def test_failed_image_skipped():
    """Phase 9.1 失败的图（_describe_failed=True）不进 embedding。"""
    row = {
        "title": "T",
        "contents": "x",
        "images": [{
            "path": "fail.jpg",
            "caption_zh": "",
            "caption_en": "",
            "entities": [],
            "_describe_failed": True,
            "_describe_reason": "parse_error",
        }],
    }
    out = compose_doc_embedding_text(row)
    assert "[图片]" not in out


def test_salvaged_image_still_participates():
    """Phase 9.1 salvaged 图（caption_zh 完整、entities 空）应继续参与。"""
    row = {
        "title": "T",
        "contents": "x",
        "images": [{
            "path": "sav.jpg",
            "caption_zh": "Salvaged 内容",
            "caption_en": "",
            "entities": [],  # salvage 时清空
        }],
    }
    out = compose_doc_embedding_text(row)
    assert "[图片]" in out
    assert "Salvaged 内容" in out
    # 没有 entities 时不出现"实体:"
    assert "实体:" not in out


def test_caption_en_only_still_participates():
    """只有英文 caption 也参与（中文 Gemini 失败但英文成功的边界）。"""
    row = {
        "title": "T", "contents": "x",
        "images": [{
            "path": "p", "caption_zh": "", "caption_en": "English only",
            "entities": [],
        }],
    }
    out = compose_doc_embedding_text(row)
    assert "English only" in out


def test_image_with_empty_captions_skipped():
    """caption_zh 和 caption_en 都空、entities 也空 → 跳过。"""
    row = {
        "title": "T", "contents": "x",
        "images": [{"path": "p", "caption_zh": "", "caption_en": "",
                    "entities": []}],
    }
    out = compose_doc_embedding_text(row)
    assert "[图片]" not in out


def test_mixed_images_only_valid_ones_appear():
    """混合：失败 + salvaged + 完整 → 只 salvaged 和完整出现。"""
    row = {
        "title": "T", "contents": "x",
        "images": [
            {"path": "fail.jpg", "caption_zh": "", "caption_en": "",
             "entities": [], "_describe_failed": True},
            {"path": "sav.jpg", "caption_zh": "salvaged caption",
             "caption_en": "", "entities": []},
            {"path": "ok.jpg", "caption_zh": "完整 caption",
             "caption_en": "ok caption", "entities": ["x", "y"]},
        ],
    }
    out = compose_doc_embedding_text(row)
    assert out.count("[图片]") == 2  # 失败的不算
    assert "salvaged caption" in out
    assert "完整 caption" in out


# ---------------------------------------------------------------------------
# 实体处理
# ---------------------------------------------------------------------------


def test_entities_dedup_and_max_8():
    """实体超 8 个截断到 8；重复实体去重。"""
    row = {
        "title": "T", "contents": "x",
        "images": [{
            "path": "p", "caption_zh": "x",
            "entities": [
                "A", "B", "C", "D", "E", "F", "G", "H", "I", "J",  # 10 个
                "A", "B",  # 重复
            ],
        }],
    }
    block = _compose_image_block(row)
    # 提取实体段
    assert "实体:" in block
    entities_part = block.split("实体: ")[1]
    # 用顿号切，应该是前 8 个去重后的
    ents = entities_part.split("、")
    assert len(ents) == 8
    assert ents == ["A", "B", "C", "D", "E", "F", "G", "H"]


def test_entities_empty_no_label():
    """entities 为空 → 不输出"实体:"标签。"""
    row = {
        "title": "T", "contents": "x",
        "images": [{"path": "p", "caption_zh": "z", "entities": []}],
    }
    block = _compose_image_block(row)
    assert "[图片]" in block
    assert "实体:" not in block


# ---------------------------------------------------------------------------
# 拼接顺序：image_block 在 body 之后
# ---------------------------------------------------------------------------


def test_image_block_appended_after_body():
    row = {
        "context": "ctx",
        "structure": {"heading_path": ["H"]},
        "title": "T", "contents": "body text",
        "images": [{"path": "p", "caption_zh": "图描述", "entities": ["e"]}],
    }
    out = compose_doc_embedding_text(row)
    # 顺序：ctx > H > T > body > image_block
    lines = out.split("\n")
    body_idx = next(i for i, l in enumerate(lines) if l == "body text")
    img_idx = next(i for i, l in enumerate(lines) if l.startswith("[图片]"))
    assert img_idx > body_idx
