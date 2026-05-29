"""Phase 11.3 双层扩展（Layer 1 邻居 + Layer 2 STEP 门卫）单元测试。

覆盖场景（对应方案 docs/TECH_DEBT_RAG_RUNNER_HARDCODE.md §六 实施清单）：
  1. doc 含 ≥ STEP_HEAVY_DOC_THRESHOLD 个 STEP → 命中后走 Layer 2 整本扩展
  2. doc 无 STEP（告警 SOP 类）→ Layer 2 门卫拒绝，不触发整本扩展
  3. 短 chunk → 走 Layer 1 邻居扩展（按 prev/next_chunk_id 链）
  4. 长 chunk → Layer 1 跳过
  5. 邻居链跨 doc 边界防御（prev/next_chunk_id 指向别家 doc 时丢弃）
  6. _compute_step_heavy_docs：阈值边界（恰好 5 vs 4）
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from custom_app.services.rag_runner import (
    NEIGHBOR_EXPAND_MAX_LEN,
    NEIGHBOR_EXPAND_MIN_LEN,
    STEP_HEAVY_DOC_THRESHOLD,
    RagRunner,
)


def _make_runner(rows: List[Dict[str, Any]]) -> RagRunner:
    """构造跳过 init() 的 stub Runner，模拟 _rows 加载后的状态。"""
    r = RagRunner.__new__(RagRunner)
    r.kb_id = "test_kb"
    r._rows = rows
    r._id_to_row = r._build_id_to_row_index()
    r._step_heavy_docs = r._compute_step_heavy_docs()
    return r


# ---------------------------------------------------------------------------
# Layer 2 门卫：_compute_step_heavy_docs 阈值
# ---------------------------------------------------------------------------


def test_step_heavy_docs_includes_doc_at_threshold():
    """恰好 STEP_HEAVY_DOC_THRESHOLD 个 STEP 块的 doc 进入门卫集合。"""
    rows: List[Dict[str, Any]] = [
        {"id": f"DocA_step_{i}", "doc": "DocA", "title": f"DocA | STEP {i}", "contents": "x"}
        for i in range(1, STEP_HEAVY_DOC_THRESHOLD + 1)
    ]
    r = _make_runner(rows)
    assert "DocA" in r._step_heavy_docs


def test_step_heavy_docs_excludes_doc_below_threshold():
    """STEP 块数不足阈值的 doc 不入门卫（agv_demo 19 份告警 SOP 全是这种）。"""
    rows: List[Dict[str, Any]] = [
        {"id": f"DocB_step_{i}", "doc": "DocB", "title": f"DocB | STEP {i}", "contents": "x"}
        for i in range(1, STEP_HEAVY_DOC_THRESHOLD)
    ]
    r = _make_runner(rows)
    assert "DocB" not in r._step_heavy_docs


def test_step_heavy_docs_excludes_zero_step_doc():
    """零 STEP 文档（intro + section_N_part_M 结构，如 ifs_docs）不入门卫。"""
    rows: List[Dict[str, Any]] = [
        {"id": "DocC_intro", "doc": "DocC", "title": "DocC", "contents": "intro"},
        {"id": "DocC_section_1", "doc": "DocC", "title": "DocC | sec", "contents": "sec1"},
        {"id": "DocC_section_2", "doc": "DocC", "title": "DocC | sec", "contents": "sec2"},
    ]
    r = _make_runner(rows)
    assert "DocC" not in r._step_heavy_docs


# ---------------------------------------------------------------------------
# Layer 2 门卫：_docs_to_expand 的 allow_only 拦截
# ---------------------------------------------------------------------------


def _build_step_heavy_doc(prefix: str, n_steps: int = STEP_HEAVY_DOC_THRESHOLD) -> List[Dict[str, Any]]:
    """构造一个含 n_steps 个 STEP 块 + intro 的 doc。"""
    rows: List[Dict[str, Any]] = [
        {"id": f"{prefix}_intro", "doc": prefix, "title": f"{prefix}", "contents": "intro"}
    ]
    for i in range(1, n_steps + 1):
        rows.append({
            "id": f"{prefix}_step_{i}",
            "doc": prefix,
            "title": f"{prefix} | STEP {i}",
            "contents": f"step {i} body",
        })
    return rows


def test_layer2_expands_step_heavy_doc_in_quick_mode():
    """STEP-heavy doc 命中 STEP 块 → quick 模式触发整本扩展。"""
    rows = _build_step_heavy_doc("BatteryChange")  # 5 个 STEP
    r = _make_runner(rows)
    # 命中两个 STEP（MIN_STEPS_FOR_EXPAND=2 触发条件）
    hit_step1_idx = r._id_to_row["BatteryChange_step_1"]
    hit_step2_idx = r._id_to_row["BatteryChange_step_2"]
    hit_ids, expanded = r._expand_hit_ids(
        [hit_step1_idx, hit_step2_idx], "怎么换电池", agent_mode="quick"
    )
    assert expanded == ["BatteryChange"]
    # 扩展后应覆盖该 doc 全部 chunk（intro + 5 个 STEP）
    assert len(hit_ids) == len(rows)


def test_layer2_blocks_non_step_heavy_doc_in_quick_mode():
    """非 STEP-heavy doc 即便命中流程意图也不触发整本扩展（门卫拦截）。"""
    # 仅 2 个 STEP，未达阈值
    rows = _build_step_heavy_doc("Alarm", n_steps=2)
    r = _make_runner(rows)
    hit_step1_idx = r._id_to_row["Alarm_step_1"]
    hit_step2_idx = r._id_to_row["Alarm_step_2"]
    hit_ids, expanded = r._expand_hit_ids(
        [hit_step1_idx, hit_step2_idx], "怎么处理告警", agent_mode="quick"
    )
    # 门卫拒绝 → 不扩展（expanded 为空，hit_ids 保持原顺序）
    assert expanded == []
    assert hit_ids == [hit_step1_idx, hit_step2_idx]


def test_layer2_gate_does_not_affect_agent_mode():
    """agent 模式不受 step-heavy 门卫限制（用户主动深读）。"""
    rows = _build_step_heavy_doc("Alarm", n_steps=2)
    r = _make_runner(rows)
    hit_ids, expanded = r._expand_hit_ids(
        [r._id_to_row["Alarm_step_1"]], "depth read", agent_mode="agent"
    )
    assert expanded == ["Alarm"]


# ---------------------------------------------------------------------------
# Layer 1 邻居扩展：短 chunk 触发 + 长 chunk 跳过 + doc 边界防御
# ---------------------------------------------------------------------------


def _short(n: int) -> str:
    """生成 n 字符的占位字符串。"""
    return "短" * n


def _long(n: int) -> str:
    return "长" * n


def test_layer1_expands_short_chunk_with_neighbors():
    """短 chunk 命中 → 按 prev/next_chunk_id 链补足语境。"""
    rows = [
        {"id": "A_1", "doc": "A", "title": "A1", "contents": _short(100),
         "prev_chunk_id": "", "next_chunk_id": "A_2"},
        {"id": "A_2", "doc": "A", "title": "A2", "contents": _short(100),
         "prev_chunk_id": "A_1", "next_chunk_id": "A_3"},
        {"id": "A_3", "doc": "A", "title": "A3", "contents": _short(200),
         "prev_chunk_id": "A_2", "next_chunk_id": ""},
    ]
    r = _make_runner(rows)
    # 命中 A_2（100 字 < min_len 350，触发扩展）
    expanded = r._expand_short_chunks_with_neighbors([r._id_to_row["A_2"]])
    # 应补入 A_1 + A_3 邻居（合并后 400 字 ≥ 350）
    assert set(expanded) == {0, 1, 2}
    # 原命中保持在第一位
    assert expanded[0] == r._id_to_row["A_2"]


def test_layer1_skips_long_chunk():
    """长 chunk（≥ min_len）不触发邻居扩展。"""
    rows = [
        {"id": "A_1", "doc": "A", "title": "A1", "contents": _short(100),
         "prev_chunk_id": "", "next_chunk_id": "A_2"},
        {"id": "A_2", "doc": "A", "title": "A2", "contents": _long(NEIGHBOR_EXPAND_MIN_LEN + 10),
         "prev_chunk_id": "A_1", "next_chunk_id": ""},
    ]
    r = _make_runner(rows)
    expanded = r._expand_short_chunks_with_neighbors([r._id_to_row["A_2"]])
    # 长 chunk 不补邻居 → hit_ids 不变
    assert expanded == [r._id_to_row["A_2"]]


def test_layer1_respects_doc_boundary():
    """邻居链跨 doc 边界时丢弃（防御 parser 注入错误的 chunk_id）。"""
    rows = [
        # base 是 DocA，但 next_chunk_id 故意指向 DocB（防御性测试）
        {"id": "A_only", "doc": "DocA", "title": "A", "contents": _short(100),
         "prev_chunk_id": "", "next_chunk_id": "B_only"},
        {"id": "B_only", "doc": "DocB", "title": "B", "contents": _short(200),
         "prev_chunk_id": "A_only", "next_chunk_id": ""},
    ]
    r = _make_runner(rows)
    expanded = r._expand_short_chunks_with_neighbors([0])
    # B_only 在不同 doc，不应被纳入；hit_ids 只剩自身
    assert expanded == [0]


def test_layer1_stops_at_max_len_cap():
    """合并接近 max_len 时停止扩展。"""
    # 三个长 chunk，base 280 字，prev 280，next 280；280+280=560 < 850，再加 280=840 < 850
    # 但 max_len 路径要求合并不超过 850，确认能正确收口
    rows = [
        {"id": "A_1", "doc": "A", "title": "A1", "contents": _short(280),
         "prev_chunk_id": "", "next_chunk_id": "A_2"},
        {"id": "A_2", "doc": "A", "title": "A2", "contents": _short(280),
         "prev_chunk_id": "A_1", "next_chunk_id": "A_3"},
        {"id": "A_3", "doc": "A", "title": "A3", "contents": _short(280),
         "prev_chunk_id": "A_2", "next_chunk_id": ""},
    ]
    r = _make_runner(rows)
    expanded = r._expand_short_chunks_with_neighbors([1])  # base A_2 (280)
    # 应至少补入一个邻居达到 min_len 350，但不能超过 max_len 850
    # base+prev=560 已 ≥ min_len 350 → 应停止
    assert set(expanded) >= {1}
    total_len = sum(
        len(rows[i]["contents"]) for i in expanded if 0 <= i < len(rows)
    )
    assert total_len <= NEIGHBOR_EXPAND_MAX_LEN


def test_layer1_no_op_on_empty_input():
    """空 hit_ids 直接返回空列表（边界保护）。"""
    r = _make_runner([])
    assert r._expand_short_chunks_with_neighbors([]) == []


def test_layer1_handles_missing_chunk_id_links():
    """prev/next_chunk_id 为空时不报错（首尾 chunk 的正常情况）。"""
    rows = [
        {"id": "lonely", "doc": "A", "title": "L", "contents": _short(100),
         "prev_chunk_id": "", "next_chunk_id": ""},
    ]
    r = _make_runner(rows)
    expanded = r._expand_short_chunks_with_neighbors([0])
    assert expanded == [0]


def test_layer1_handles_dangling_chunk_id():
    """prev/next_chunk_id 指向不存在的 chunk（增量 parse 边界）→ 链终止不崩。"""
    rows = [
        {"id": "A_1", "doc": "A", "title": "A1", "contents": _short(100),
         "prev_chunk_id": "ghost", "next_chunk_id": ""},
    ]
    r = _make_runner(rows)
    expanded = r._expand_short_chunks_with_neighbors([0])
    assert expanded == [0]
