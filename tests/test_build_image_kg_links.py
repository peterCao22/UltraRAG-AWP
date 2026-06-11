"""Phase 9.3.A build_image_kg_links 单元测试 — resolver 模糊匹配逻辑。

覆盖：
  1. 完全匹配（大小写 / 空白归一化）
  2. 包含匹配：图实体 ⊆ KG 实体
  3. 包含匹配：KG 实体 ⊆ 图实体
  4. 太短的图实体跳过包含匹配
  5. 没匹配返空
  6. 完全匹配优先于包含
  7. 多个 KG 候选时全部返回（去重 + 保序）
  8. _make_img_id 稳定性
  9. _normalize_for_match 规则
"""

from __future__ import annotations

from custom_app.scripts.build_image_kg_links import (
    _make_img_id,
    _normalize_for_match,
    _resolve_entity_to_kg_names,
)


# ---------------------------------------------------------------------------
# _normalize_for_match
# ---------------------------------------------------------------------------


def test_normalize_strips_whitespace_and_lowercases() -> None:
    assert _normalize_for_match("AGV Control Panel") == "agvcontrolpanel"
    assert _normalize_for_match("  急停 按钮 ") == "急停按钮"
    assert _normalize_for_match("") == ""


# ---------------------------------------------------------------------------
# _resolve_entity_to_kg_names：完全匹配
# ---------------------------------------------------------------------------


def test_exact_match_returns_kg_name() -> None:
    out = _resolve_entity_to_kg_names("急停按钮", ["急停按钮", "AGV", "其他"])
    assert out == ["急停按钮"]


def test_exact_match_case_insensitive_english() -> None:
    """图实体 'agv' 应匹配 KG 实体 'AGV'。"""
    out = _resolve_entity_to_kg_names("agv", ["AGV"])
    assert out == ["AGV"]


def test_exact_match_whitespace_insensitive() -> None:
    out = _resolve_entity_to_kg_names("master link down", ["MasterLinkDown"])
    assert out == ["MasterLinkDown"]


# ---------------------------------------------------------------------------
# 包含匹配
# ---------------------------------------------------------------------------


def test_substring_img_in_kg() -> None:
    """图实体'控制面板' 应匹配 KG 实体'AGV 控制面板'。"""
    out = _resolve_entity_to_kg_names("控制面板", ["AGV 控制面板", "其他实体"])
    assert out == ["AGV 控制面板"]


def test_substring_kg_in_img() -> None:
    """KG 实体'Master Link' 应被图实体'Master Link Down' 包含→匹配。"""
    out = _resolve_entity_to_kg_names("Master Link Down", ["Master Link", "ABC"])
    assert out == ["Master Link"]


def test_substring_returns_multiple_kg_matches() -> None:
    """图实体'急停' 应同时匹配'急停按钮' 和'急停开关'。"""
    out = _resolve_entity_to_kg_names("急停", ["急停按钮", "急停开关", "其他"])
    assert out == ["急停按钮", "急停开关"]


# ---------------------------------------------------------------------------
# 完全匹配优先
# ---------------------------------------------------------------------------


def test_exact_match_takes_precedence_over_substring() -> None:
    """若完全匹配存在，包含匹配不执行。"""
    out = _resolve_entity_to_kg_names("急停", ["急停", "急停按钮"])
    # 只返完全匹配的"急停"，不返"急停按钮"
    assert out == ["急停"]


# ---------------------------------------------------------------------------
# 太短跳过包含匹配
# ---------------------------------------------------------------------------


def test_too_short_skips_fuzzy() -> None:
    """图实体'A' 长度 1 < min_fuzzy_len=2，不进入包含匹配，返空。"""
    out = _resolve_entity_to_kg_names("A", ["ABC", "AGV"])
    assert out == []


def test_min_fuzzy_len_threshold_custom() -> None:
    """自定义阈值：min_fuzzy_len=4 时'AGV'（归一化 3 字符）也跳过。"""
    out = _resolve_entity_to_kg_names(
        "AGV", ["AGV Control", "Other"], min_fuzzy_len=4,
    )
    # 完全匹配优先（"AGV" ≠ "AGV Control" 归一化），无完全匹配；
    # 长度 3 < 4 → 跳过包含 → 返空
    assert out == []


# ---------------------------------------------------------------------------
# 无匹配
# ---------------------------------------------------------------------------


def test_no_match_returns_empty() -> None:
    out = _resolve_entity_to_kg_names("数字键盘", ["AGV", "Master Link"])
    assert out == []


def test_empty_img_entity_returns_empty() -> None:
    out = _resolve_entity_to_kg_names("", ["AGV"])
    assert out == []


def test_empty_kg_list_returns_empty() -> None:
    out = _resolve_entity_to_kg_names("急停按钮", [])
    assert out == []


# ---------------------------------------------------------------------------
# 去重 + 保序
# ---------------------------------------------------------------------------


def test_duplicate_kg_entities_dedup() -> None:
    """KG 列表里实体重复（不会发生但防御性）→ 仅返一次。"""
    out = _resolve_entity_to_kg_names("急停", ["急停按钮", "急停按钮"])
    assert out == ["急停按钮"]


def test_preserves_kg_list_order() -> None:
    """多匹配按 KG 列表里的出现顺序返回。"""
    out = _resolve_entity_to_kg_names("AGV", ["AGV-2 Module", "AGV Control Panel"])
    assert out == ["AGV-2 Module", "AGV Control Panel"]


# ---------------------------------------------------------------------------
# _make_img_id 稳定性
# ---------------------------------------------------------------------------


def test_make_img_id_stable() -> None:
    """同 (kb_id, path) 始终得相同 img_id。"""
    a = _make_img_id("agv_demo", "images/foo.jpg")
    b = _make_img_id("agv_demo", "images/foo.jpg")
    assert a == b
    assert len(a) == 12  # sha1[:12]


def test_make_img_id_differs_by_kb() -> None:
    """不同 kb_id 应得不同 img_id（避免跨 KB 节点撞 id）。"""
    a = _make_img_id("agv_demo", "images/foo.jpg")
    b = _make_img_id("ifs_docs", "images/foo.jpg")
    assert a != b


def test_make_img_id_differs_by_path() -> None:
    a = _make_img_id("agv_demo", "images/foo.jpg")
    b = _make_img_id("agv_demo", "images/bar.jpg")
    assert a != b
