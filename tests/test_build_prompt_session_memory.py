"""Phase 12.2 _build_prompt 集成：prior_summary + recent_turns 拼模板。

只测 _build_prompt 渲染输出含/不含这些段，不依赖 LLM / DB。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from custom_app.services.rag_runner import RagRunner


@pytest.fixture()
def runner():
    """stub Runner，只设置 _rows + prompt_dir 让 _build_prompt 可跑。"""
    r = RagRunner.__new__(RagRunner)
    r.kb_id = "test_kb"
    r._rows = [
        {
            "id": "doc_intro",
            "doc": "DocA",
            "title": "DocA | intro",
            "contents": "AGV 启动前需检查电池、急停按钮、导航传感器。",
        }
    ]
    r.prompt_dir = Path("prompt")
    return r


def test_prompt_excludes_memory_sections_by_default(runner):
    text = runner._build_prompt("怎么启动？", [0])
    # 默认空 summary + 空 recent_turns → 不应包含历史段
    assert "Prior conversation summary" not in text
    assert "Recent dialogue turns" not in text
    # 模板主体仍正常渲染
    assert "AGV 启动前" in text


def test_prompt_includes_prior_summary_when_provided(runner):
    text = runner._build_prompt(
        "怎么启动？",
        [0],
        prior_summary="用户在咨询 AGV 启动检查；系统已列三项检查。",
    )
    assert "Prior conversation summary" in text
    assert "AGV 启动检查" in text


def test_prompt_includes_recent_turns_when_provided(runner):
    text = runner._build_prompt(
        "第 2 个怎么操作？",
        [0],
        recent_turns=[
            {"role_label": "User", "content": "AGV 启动前要做什么？"},
            {"role_label": "Assistant", "content": "1. 电池 2. 急停按钮 3. 导航"},
        ],
    )
    assert "Recent dialogue turns" in text
    assert "AGV 启动前要做什么？" in text
    assert "1. 电池 2. 急停按钮 3. 导航" in text


def test_prompt_includes_both_when_both_provided(runner):
    text = runner._build_prompt(
        "继续",
        [0],
        prior_summary="历史摘要…",
        recent_turns=[
            {"role_label": "User", "content": "上一轮问题"},
        ],
    )
    assert "Prior conversation summary" in text
    assert "历史摘要" in text
    assert "Recent dialogue turns" in text
    assert "上一轮问题" in text


def test_prompt_question_still_present_with_memory(runner):
    """无论是否注入 memory，模板的 User question 字段始终是当前 question。"""
    q = "急停按钮怎么测？"
    text = runner._build_prompt(
        q, [0],
        prior_summary="历史",
        recent_turns=[{"role_label": "User", "content": "上一轮"}],
    )
    assert q in text


def test_prompt_strips_empty_summary(runner):
    """prior_summary 是空白时不渲染 prior_summary 段。"""
    text = runner._build_prompt("test", [0], prior_summary="   ")
    assert "Prior conversation summary" not in text
