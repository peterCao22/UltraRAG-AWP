"""Phase 8.2.2.a BM25Store 测试。

覆盖：
    - tokenize：中英混合 / 空输入
    - from_rows：构造 + 跳过空 id 行
    - search：基础命中 / top_k 截断 / 空 query / 中文短语
    - _row_to_index_text：context / heading_path / title / contents 拼接
    - _strip_image_placeholders：剔除 [IMG: ...] 占位
"""
from __future__ import annotations

import pytest

from custom_app.services.retrieval.bm25 import (
    BM25Store,
    _row_to_index_text,
    _strip_image_placeholders,
    tokenize,
)


# ─────────────────────────────────────────────────────────────────────────────
# 纯函数
# ─────────────────────────────────────────────────────────────────────────────


class TestTokenize:
    def test_mixed_chinese_english(self) -> None:
        out = tokenize("AGV 启动 STEP 3")
        # 一定含英文小写 token
        assert "agv" in out
        assert "step" in out
        # 中文必有"启动"
        assert "启动" in out

    def test_empty_input(self) -> None:
        assert tokenize("") == []
        assert tokenize(None) == []  # type: ignore[arg-type]

    def test_pure_english(self) -> None:
        out = tokenize("Battery Change Sequence")
        assert "battery" in out
        assert "change" in out
        assert "sequence" in out

    def test_pure_chinese(self) -> None:
        out = tokenize("急停按钮被按下")
        # jieba 应至少切出"急停"或"按钮"
        joined = " ".join(out)
        assert "急停" in joined or "按钮" in joined


class TestStripImagePlaceholders:
    def test_drops_img_lines(self) -> None:
        text = "STEP 1\n[IMG: x.png]\n按下按钮"
        assert _strip_image_placeholders(text) == "STEP 1\n按下按钮"

    def test_empty_safe(self) -> None:
        assert _strip_image_placeholders("") == ""
        assert _strip_image_placeholders(None) == ""  # type: ignore[arg-type]


class TestRowToIndexText:
    def test_full_assembly(self) -> None:
        row = {
            "id": "x",
            "context": "本文档介绍 XYZ AGV 启动",
            "structure": {"heading_path": ["AGV 启动手册"]},
            "title": "AGV 启动手册 | STEP 1",
            "contents": "STEP 1: 检查电池\n[IMG: x.png]",
        }
        text = _row_to_index_text(row)
        # 四段都应出现，且 [IMG: ...] 被剥离
        assert "本文档介绍" in text
        assert "AGV 启动手册" in text
        assert "STEP 1" in text
        assert "[IMG" not in text

    def test_missing_context_is_optional(self) -> None:
        row = {"id": "x", "title": "T", "contents": "body"}
        text = _row_to_index_text(row)
        assert "T" in text and "body" in text


# ─────────────────────────────────────────────────────────────────────────────
# BM25Store
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def _sample_rows() -> list[dict]:
    return [
        {
            "id": "agv_battery_step_1",
            "title": "AGV 换电 STEP 1",
            "contents": "拆下旧电池，检查接口是否完好",
        },
        {
            "id": "agv_battery_step_2",
            "title": "AGV 换电 STEP 2",
            "contents": "装入新电池，按下绿色启动按钮",
        },
        {
            "id": "ifs_estop_info",
            "title": "IFS E-Stop 处理",
            "contents": "E-Stop Button Active 告警出现时，先确认急停按钮位置",
        },
        {
            "id": "ifs_login",
            "title": "IFS 登录",
            "contents": "客户端 404 报错时，检查 server URL 与 VPN 状态",
        },
    ]


class TestBM25Store:
    def test_from_rows_indexes_all_valid(self, _sample_rows: list[dict]) -> None:
        store = BM25Store.from_rows(_sample_rows)
        assert store.size() == 4

    def test_from_rows_skips_empty_ids(self) -> None:
        rows = [
            {"id": "", "contents": "ignored"},
            {"id": "ok", "contents": "real chunk"},
        ]
        store = BM25Store.from_rows(rows)
        assert store.size() == 1

    def test_from_rows_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="no valid rows"):
            BM25Store.from_rows([])

    def test_chinese_query_hits_chinese_chunk(self, _sample_rows: list[dict]) -> None:
        store = BM25Store.from_rows(_sample_rows)
        hits = store.search("装入新电池", top_k=3)
        assert hits  # 至少有命中
        # 第一个应是 step_2
        assert hits[0].chunk_id == "agv_battery_step_2"

    def test_english_query_hits_english_chunk(self, _sample_rows: list[dict]) -> None:
        store = BM25Store.from_rows(_sample_rows)
        hits = store.search("E-Stop Button Active", top_k=3)
        assert hits
        assert hits[0].chunk_id == "ifs_estop_info"

    def test_mixed_query(self, _sample_rows: list[dict]) -> None:
        """中英混合 query，区分性词（"电池"）只在 agv 系列文档出现 → 应命中 battery 系列。"""
        store = BM25Store.from_rows(_sample_rows)
        # 注意：BM25 的 IDF 让所有 chunk 都含的词（如"AGV"在 ifs 文档不出现，但
        # "换电池"也只在 agv 出现）才有区分度。避免选所有 chunk 都共有的词。
        hits = store.search("拆下电池接口", top_k=4)
        # 命中的两个都来自 agv_battery_step_*
        hit_ids = [h.chunk_id for h in hits]
        assert "agv_battery_step_1" in hit_ids
        # step_1 "拆下旧电池，检查接口" 命中分应高于 step_2
        assert hits[0].chunk_id == "agv_battery_step_1"

    def test_top_k_limits(self, _sample_rows: list[dict]) -> None:
        store = BM25Store.from_rows(_sample_rows)
        hits = store.search("电池", top_k=1)
        assert len(hits) <= 1

    def test_empty_query(self, _sample_rows: list[dict]) -> None:
        store = BM25Store.from_rows(_sample_rows)
        assert store.search("", top_k=5) == []
        assert store.search("ABCDEFG_nonexistent_term", top_k=5) == []

    def test_scores_are_positive_and_descending(self, _sample_rows: list[dict]) -> None:
        store = BM25Store.from_rows(_sample_rows)
        hits = store.search("AGV 电池", top_k=4)
        assert all(h.score > 0 for h in hits)
        scores = [h.score for h in hits]
        assert scores == sorted(scores, reverse=True)
