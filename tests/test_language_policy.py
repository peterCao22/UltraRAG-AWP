from __future__ import annotations

from custom_app.services.language_policy import (
    append_system_language_rule,
    answer_language_policy,
    detect_answer_language,
)


def test_english_question_uses_english():
    assert detect_answer_language("What alarm ID is triggered by right arm obstruction?") == "English"
    policy = answer_language_policy("What alarm ID is triggered by right arm obstruction?")
    assert policy.language == "English"
    assert "Do not translate" in policy.instruction


def test_chinese_question_uses_simplified_chinese():
    assert detect_answer_language("右臂有障碍物时触发哪个报警 ID？") == "Simplified Chinese"
    policy = answer_language_policy("右臂有障碍物时触发哪个报警 ID？")
    assert policy.language == "Simplified Chinese"
    assert "Translate English source text" in policy.instruction


def test_explicit_translation_target_wins():
    assert detect_answer_language("请把这段内容翻译成英文：右臂报警") == "English"
    assert (
        detect_answer_language("Translate this answer into Chinese: Right FTC Alarm")
        == "Simplified Chinese"
    )


def test_append_system_language_rule_once():
    prompt = append_system_language_rule("Always be concise.")
    assert "Always be concise." in prompt
    assert "English questions must receive English answers" in prompt
    assert append_system_language_rule(prompt) == prompt
