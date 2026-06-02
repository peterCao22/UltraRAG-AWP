"""Language selection helpers for chat answers."""

from __future__ import annotations

import re
from dataclasses import dataclass


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

_TO_EN_RE = re.compile(
    r"(?:translate|render|convert|answer|reply|respond)\b.*\b(?:to|into|in)\s+"
    r"(?:english|en\b)|"
    r"(?:翻译|译成|翻成|转成|用|使用).{0,16}(?:英文|英语)",
    re.IGNORECASE,
)
_TO_ZH_RE = re.compile(
    r"(?:translate|render|convert|answer|reply|respond)\b.*\b(?:to|into|in)\s+"
    r"(?:chinese|simplified chinese|mandarin|zh\b)|"
    r"(?:翻译|译成|翻成|转成|用|使用).{0,16}(?:中文|汉语|简体中文|简体)",
    re.IGNORECASE,
)

AUTO_LANGUAGE_POLICY = (
    "auto (match the user's latest question; translate only when explicitly requested)"
)
SYSTEM_LANGUAGE_RULE = (
    "Language rule: reply in the same language as the user's latest question. "
    "Chinese questions must receive Simplified Chinese answers; English questions "
    "must receive English answers. Only translate between Chinese and English when "
    "the user explicitly asks for translation, and then follow the requested direction."
)


@dataclass(frozen=True)
class AnswerLanguagePolicy:
    language: str
    no_information_text: str
    instruction: str


def detect_answer_language(question: str) -> str:
    """Return the language the final answer should use."""
    q = (question or "").strip()
    if _TO_EN_RE.search(q):
        return "English"
    if _TO_ZH_RE.search(q):
        return "Simplified Chinese"
    if _CJK_RE.search(q):
        return "Simplified Chinese"
    return "English"


def answer_language_policy(question: str) -> AnswerLanguagePolicy:
    """Build prompt-ready language instructions for a user question."""
    language = detect_answer_language(question)
    if language == "English":
        return AnswerLanguagePolicy(
            language=language,
            no_information_text=(
                "Based on the available documents, no information relevant to this "
                "question was found, so I cannot answer."
            ),
            instruction=(
                "Answer in English. Do not translate the answer into Chinese unless "
                "the user explicitly requested Chinese translation."
            ),
        )
    return AnswerLanguagePolicy(
        language=language,
        no_information_text="根据现有文档，未找到与该问题相关的信息，无法回答。",
        instruction=(
            "Answer in Simplified Chinese. Translate English source text into natural "
            "Simplified Chinese as needed."
        ),
    )


def append_system_language_rule(system_prompt: str) -> str:
    """Append the shared language rule to a system prompt once."""
    prompt = (system_prompt or "").strip()
    marker = "reply in the same language as the user's latest question"
    if marker.lower() in prompt.lower():
        return prompt
    if not prompt:
        return SYSTEM_LANGUAGE_RULE
    return f"{prompt}\n\n{SYSTEM_LANGUAGE_RULE}"
