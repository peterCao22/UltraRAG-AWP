"""Phase 8.3 Week 1 IRCoT 多轮检索单元测试。

策略：mock RagRunner 的 _prepare_chat_context / _generate / _rows，
不真起 Qdrant / Gemini；验证：
    - 单跳：第 1 轮思考含"答案是"→ 立刻终止，n_loops=1
    - 多跳：第 1 轮未终止 → 用首句再检索 → 第 2 轮终止
    - max_loops 达上限：取最后一次思考作答
    - chunks_seen 去重 + 保留首次出现顺序
    - _extract_final_answer / _first_sentence / _has_end_marker 纯函数
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from custom_app.services.strategies.ircot import (
    _END_PATTERNS,
    _extract_final_answer,
    _first_sentence,
    _has_end_marker,
    chat_ircot,
)


# ─────────────────────────────────────────────────────────────────────────────
# 纯函数
# ─────────────────────────────────────────────────────────────────────────────


class TestHasEndMarker:
    @pytest.mark.parametrize(
        "text",
        [
            "答案是：检查急停按钮",
            "因此答案是 ID 01",
            "所以答案为 Master Link Down",
            "最终答案：按 7 号键",
            "So the answer is: press 7",
            "Thus the answer is ID 01",
        ],
    )
    def test_positive(self, text: str) -> None:
        assert _has_end_marker(text)

    @pytest.mark.parametrize(
        "text",
        [
            "我还需要查另一个文档",
            "I need to check the next section",
            "",
            "片段 1 显示...",
        ],
    )
    def test_negative(self, text: str) -> None:
        assert not _has_end_marker(text)


class TestExtractFinalAnswer:
    def test_chinese_pattern(self) -> None:
        assert _extract_final_answer("答案是：检查两侧急停按钮") == "检查两侧急停按钮"

    def test_english_pattern(self) -> None:
        assert _extract_final_answer("So the answer is: press 7 to navigate") == "press 7 to navigate"

    def test_no_pattern_returns_text(self) -> None:
        assert _extract_final_answer("仅一段思考没有标记") == "仅一段思考没有标记"

    def test_empty(self) -> None:
        assert _extract_final_answer("") == ""


class TestFirstSentence:
    def test_chinese_first_sentence(self) -> None:
        assert _first_sentence("我需要查另一个章节。然后再总结。") == "我需要查另一个章节。"

    def test_english_first_sentence(self) -> None:
        assert _first_sentence("I need to check Section 2. Then synthesize.") == "I need to check Section 2."

    def test_strip_thought_prefix(self) -> None:
        out = _first_sentence("思考：先看 Excerpt 1，发现需要补 Master Link Down 文档。然后...")
        assert "先看 Excerpt 1" in out

    def test_none_on_empty(self) -> None:
        assert _first_sentence("") is None
        assert _first_sentence(None) is None  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────────
# chat_ircot 集成（stub RagRunner）
# ─────────────────────────────────────────────────────────────────────────────


def _make_stub_runner(
    rows: list[dict],
    *,
    query_to_hits: dict[str, list[int]],
    llm_responses: list[str],
    prompt_dir: Path,
) -> MagicMock:
    """构造一个 stub RagRunner 实例。

    Args:
        rows: 模拟 self._rows
        query_to_hits: query → 行号列表（_prepare_chat_context 返回的 hit_ids）
        llm_responses: 按调用顺序返回的 LLM 回答（_generate）
    """
    runner = MagicMock()
    runner._rows = rows
    runner.prompt_dir = prompt_dir

    def _fake_prepare(q: str, top_k: int | None = None, agent_mode: str = "quick") -> dict:
        hits = query_to_hits.get(q, query_to_hits.get(q.strip(), []))
        if top_k is not None:
            hits = hits[: int(top_k)]
        return {"hit_ids": list(hits)}

    runner._prepare_chat_context = MagicMock(side_effect=_fake_prepare)

    responses_iter = iter(llm_responses)

    def _fake_generate(prompt_text: str) -> str:
        try:
            return next(responses_iter)
        except StopIteration:
            return llm_responses[-1] if llm_responses else ""

    runner._generate = MagicMock(side_effect=_fake_generate)
    return runner


@pytest.fixture()
def prompt_dir() -> Path:
    # 用项目真实 prompt 目录（含 ircot_sop.jinja）
    return Path("prompt")


class TestChatIrcotSingleHop:
    def test_first_thought_with_end_marker_stops_immediately(
        self, prompt_dir: Path
    ) -> None:
        rows = [
            {"id": "estop_intro", "contents": "ID 01 = E-Stop Button Active"},
            {"id": "other", "contents": "noise"},
        ]
        runner = _make_stub_runner(
            rows,
            query_to_hits={"ID 01 是什么告警？": [0]},
            llm_responses=["思考：[Excerpt 1] 明确 ID 01 = E-Stop。答案是：ID 01 是 E-Stop Button Active 告警"],
            prompt_dir=prompt_dir,
        )
        out = chat_ircot(runner, "ID 01 是什么告警？", max_loops=3)
        assert out["n_loops"] == 1
        assert "E-Stop" in out["answer"]
        assert out["meta"]["early_stopped"] is True
        assert out["chunks_seen"] == ["estop_intro"]
        # _generate 只调一次
        assert runner._generate.call_count == 1


class TestChatIrcotMultiHop:
    def test_two_round_pulls_next_chunk(self, prompt_dir: Path) -> None:
        rows = [
            {"id": "battery_step_11", "contents": "STEP 11: confirm Master Link Down cleared"},
            {"id": "master_link_section_1", "contents": "Master Link Down: press blue then green"},
        ]
        # 第 1 轮 query = 用户原 query → 命中 row 0
        # 第 2 轮 query = 第 1 轮思考首句（"我需要查..."） → 命中 row 1
        # 注意 _first_sentence 抽第一个 句号/问号/感叹号 之前的内容，所以
        # 第一个 LLM response 的首句就必须是想触发下一轮检索的查询语句
        runner = _make_stub_runner(
            rows,
            query_to_hits={
                "换完电池后报 Master Link Down 怎么办？": [0],
                "我需要查 Master Link Down 专项文档。": [1],
            },
            llm_responses=[
                "思考：我需要查 Master Link Down 专项文档。Excerpt 1 显示 STEP 11 提到要确认告警消失但没说怎么消除。",
                "思考：[Excerpt 2] 给出处理方法。答案是：按蓝色再按绿色按钮重置连接",
            ],
            prompt_dir=prompt_dir,
        )
        out = chat_ircot(runner, "换完电池后报 Master Link Down 怎么办？", max_loops=3)
        assert out["n_loops"] == 2
        assert "蓝色" in out["answer"]
        assert out["chunks_seen"] == ["battery_step_11", "master_link_section_1"]
        assert out["meta"]["early_stopped"] is True

    def test_first_sentence_drives_next_round(self, prompt_dir: Path) -> None:
        """验证：第 2 轮 query 确实是第 1 轮思考的首句（首个句末标点前的内容）。"""
        rows = [
            {"id": "a", "contents": "alpha"},
            {"id": "b", "contents": "beta"},
        ]
        runner = _make_stub_runner(
            rows,
            query_to_hits={
                "原 query": [0],
                "我接下来要查 beta 文档。": [1],
            },
            llm_responses=[
                # 首句必须是要触发下一轮检索的查询语句
                "思考：我接下来要查 beta 文档。先看 alpha 然后再综合。",
                "答案是：alpha + beta 的组合",
            ],
            prompt_dir=prompt_dir,
        )
        out = chat_ircot(runner, "原 query", max_loops=3)
        # 验证第 2 轮 _prepare_chat_context 被调用、参数是首句
        prep_calls = runner._prepare_chat_context.call_args_list
        assert len(prep_calls) == 2
        assert prep_calls[1].args[0] == "我接下来要查 beta 文档。"


class TestChatIrcotEdgeCases:
    def test_max_loops_reached_uses_last_thought(self, prompt_dir: Path) -> None:
        rows = [{"id": "a", "contents": "alpha"}]
        runner = _make_stub_runner(
            rows,
            query_to_hits={"q": [0], "继续思考。": [0]},
            llm_responses=["思考：继续思考。"] * 5,  # 永不终止
            prompt_dir=prompt_dir,
        )
        out = chat_ircot(runner, "q", max_loops=2)
        assert out["n_loops"] == 2
        assert out["answer"]  # 即使没 end marker 也要有答案
        assert out["meta"]["early_stopped"] is False

    def test_empty_question_raises(self, prompt_dir: Path) -> None:
        runner = _make_stub_runner(
            rows=[{"id": "x", "contents": "x"}],
            query_to_hits={},
            llm_responses=["x"],
            prompt_dir=prompt_dir,
        )
        with pytest.raises(ValueError, match="empty"):
            chat_ircot(runner, "  ", max_loops=2)

    def test_max_loops_below_one_raises(self, prompt_dir: Path) -> None:
        runner = _make_stub_runner(
            rows=[{"id": "x", "contents": "x"}],
            query_to_hits={"q": [0]},
            llm_responses=["a"],
            prompt_dir=prompt_dir,
        )
        with pytest.raises(ValueError, match="max_loops"):
            chat_ircot(runner, "q", max_loops=0)

    def test_no_rows_raises(self, prompt_dir: Path) -> None:
        runner = _make_stub_runner(
            rows=[],
            query_to_hits={"q": []},
            llm_responses=["a"],
            prompt_dir=prompt_dir,
        )
        with pytest.raises(RuntimeError, match="_rows is empty"):
            chat_ircot(runner, "q", max_loops=2)

    def test_chunks_seen_deduplicates(self, prompt_dir: Path) -> None:
        """两轮检索都返回 row 0 → chunks_seen 仅 1 个。"""
        rows = [{"id": "a", "contents": "alpha"}]
        runner = _make_stub_runner(
            rows,
            query_to_hits={
                "q": [0],
                "next.": [0],  # 同 row 重复
            },
            llm_responses=[
                "思考：first round. next.",
                "答案是：done",
            ],
            prompt_dir=prompt_dir,
        )
        out = chat_ircot(runner, "q", max_loops=3)
        assert out["chunks_seen"] == ["a"]  # 不重复
