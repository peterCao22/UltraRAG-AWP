"""Phase 7.2.A: RagRunner / AgentRunner agent_config 覆盖单测。

仅检查 system_prompt / temperature / max_tokens 的覆盖优先级，
不真的跑 LLM。"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    monkeypatch.chdir(tmp_path)
    yield tmp_path


class TestRagRunnerAgentConfigOverride:
    def _make_runner(self, agent_config: dict | None):
        from custom_app.services.rag_runner import RagRunner

        runner = RagRunner(kb_id="agv_demo", agent_config=agent_config)
        # 不跑完整 init()，直接初始化 _chat_cfg 占位
        runner._chat_cfg = {
            "backend": "openai",
            "system_prompt": "yaml 兜底 prompt",
            "temperature": 0.2,
            "max_tokens": 4096,
        }
        return runner

    def test_no_agent_config_keeps_yaml(self):
        r = self._make_runner(None)
        r._apply_agent_config_override()
        assert r._chat_cfg["system_prompt"] == "yaml 兜底 prompt"
        assert r._chat_cfg["temperature"] == 0.2

    def test_agent_config_overrides_system_prompt(self):
        r = self._make_runner(
            {
                "agent_id": "agent_a",
                "system_prompt": "用 markdown 嵌套粗体标题排版。",
                "temperature": None,
                "max_tokens": None,
            }
        )
        r._apply_agent_config_override()
        assert r._chat_cfg["system_prompt"] == "用 markdown 嵌套粗体标题排版。"
        # 未给则保留 yaml
        assert r._chat_cfg["temperature"] == 0.2
        assert r._chat_cfg["max_tokens"] == 4096

    def test_agent_config_renders_kb_name_placeholder(self):
        r = self._make_runner(
            {
                "agent_id": "agent_a",
                "system_prompt": "你是 {{kb_name}} 的助手。语言 {{language}}",
            }
        )
        r._apply_agent_config_override()
        assert "agv_demo" in r._chat_cfg["system_prompt"]
        assert "Chinese (Simplified)" in r._chat_cfg["system_prompt"]

    def test_agent_config_temperature_max_tokens_override(self):
        r = self._make_runner(
            {
                "agent_id": "agent_a",
                "system_prompt": "",
                "temperature": 0.85,
                "max_tokens": 2048,
            }
        )
        r._apply_agent_config_override()
        assert r._chat_cfg["temperature"] == 0.85
        assert r._chat_cfg["max_tokens"] == 2048
        # 空 system_prompt 不覆盖
        assert r._chat_cfg["system_prompt"] == "yaml 兜底 prompt"

    def test_invalid_temperature_silently_ignored(self):
        r = self._make_runner(
            {
                "agent_id": "agent_a",
                "system_prompt": "",
                "temperature": "not-a-number",
                "max_tokens": "abc",
            }
        )
        r._apply_agent_config_override()
        # 不抛异常，保留兜底值
        assert r._chat_cfg["temperature"] == 0.2
        assert r._chat_cfg["max_tokens"] == 4096


class TestAgentRunnerAgentSystemPrompt:
    def test_custom_agent_system_prompt_overrides_jinja(self):
        from custom_app.services.agent_runner import AgentRunner

        # 用 __new__ 绕过 init()，仅测 _build_system_prompt
        ar = AgentRunner.__new__(AgentRunner)
        ar.kb_id = "agv_demo"
        ar._kb_name = "agv_demo"
        ar._kg_available = True
        ar._agent_config = {
            "agent_id": "agent_a",
            "agent_system_prompt": "你是 {{kb_name}} 的 ReAct agent。",
        }

        out = ar._build_system_prompt()
        assert "agv_demo" in out
        assert "ReAct agent" in out
        # 不应包含 jinja 兜底里的特有内容
        assert "渐进式 Agentic RAG" not in out

    def test_empty_agent_system_prompt_falls_back_to_jinja(self):
        from custom_app.services.agent_runner import AgentRunner

        ar = AgentRunner.__new__(AgentRunner)
        ar.kb_id = "agv_demo"
        ar._kb_name = "agv_demo"
        ar._kg_available = True
        ar._agent_config = {"agent_system_prompt": ""}

        out = ar._build_system_prompt()
        # 落到 jinja 模板，应包含其特有词
        assert "知识库" in out

    def test_no_agent_config_falls_back_to_jinja(self):
        from custom_app.services.agent_runner import AgentRunner

        ar = AgentRunner.__new__(AgentRunner)
        ar.kb_id = "agv_demo"
        ar._kb_name = "agv_demo"
        ar._kg_available = True
        ar._agent_config = None

        out = ar._build_system_prompt()
        assert "知识库" in out

    def test_kg_unavailable_appends_note(self):
        from custom_app.services.agent_runner import AgentRunner

        ar = AgentRunner.__new__(AgentRunner)
        ar.kb_id = "agv_demo"
        ar._kb_name = "agv_demo"
        ar._kg_available = False
        ar._agent_config = {"agent_system_prompt": "你是 {{kb_name}} 助手。"}

        out = ar._build_system_prompt()
        assert "知识图谱（KG）不可用" in out
