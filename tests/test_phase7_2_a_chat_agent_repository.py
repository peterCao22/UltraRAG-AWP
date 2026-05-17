"""Phase 7.2.A: ChatAgentRepository 单测（SQLite 后端）。"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir(exist_ok=True)
    from custom_app.repositories import set_default_provider

    set_default_provider(None)
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")
    yield tmp_path
    set_default_provider(None)


@pytest.fixture
def repo():
    from custom_app.db import init_db
    from custom_app.repositories import ChatAgentRepository

    init_db()
    return ChatAgentRepository()


def _make(
    repo,
    agent_id: str,
    *,
    name: str = "测试助手",
    agent_mode: str = "quick",
    is_builtin: bool = False,
    system_prompt: str = "你是一个助手。",
    model_id: str = "",
    temperature: float = 0.5,
    enabled: bool = True,
) -> None:
    repo.create(
        agent_id=agent_id,
        name=name,
        agent_mode=agent_mode,
        is_builtin=is_builtin,
        system_prompt=system_prompt,
        model_id=model_id,
        temperature=temperature,
        enabled=enabled,
        created_at="2026-05-16T00:00:00Z",
    )


class TestBuiltinSeed:
    def test_init_db_seeds_two_builtin_agents(self, repo):
        bq = repo.get_builtin_quick()
        ba = repo.get_builtin_agent()
        assert bq is not None and bq["agent_mode"] == "quick"
        assert ba is not None and ba["agent_mode"] == "agent"
        assert bq["is_builtin"] is True
        assert ba["is_builtin"] is True
        # builtin-quick 复用 AGV SOP prompt
        assert "AGV" in (bq["system_prompt"] or "")

    def test_init_db_is_idempotent(self, repo):
        # 再次跑 init_db 不应重复种子（agent_id UNIQUE）
        from custom_app.db import init_db

        init_db()
        rows = repo.list_active(include_disabled=True)
        builtin_rows = [r for r in rows if r["is_builtin"]]
        assert len(builtin_rows) == 2


class TestCRUD:
    def test_create_then_get(self, repo):
        _make(repo, "agent_a", name="商业资料助手", temperature=0.4)
        row = repo.get("agent_a")
        assert row is not None
        assert row["name"] == "商业资料助手"
        assert row["agent_mode"] == "quick"
        assert row["is_builtin"] is False
        assert row["enabled"] is True
        assert abs(row["temperature"] - 0.4) < 1e-6

    def test_list_active_orders_builtin_first(self, repo):
        _make(repo, "agent_a", name="aaa")
        active = repo.list_active()
        assert len(active) >= 3  # 2 builtin + 1 user-created
        # builtin 排在前面
        assert active[0]["is_builtin"] is True
        assert active[1]["is_builtin"] is True
        assert active[-1]["agent_id"] == "agent_a"

    def test_list_active_excludes_disabled_by_default(self, repo):
        _make(repo, "agent_disabled", enabled=False)
        active = repo.list_active()
        agent_ids = [r["agent_id"] for r in active]
        assert "agent_disabled" not in agent_ids
        all_rows = repo.list_active(include_disabled=True)
        all_ids = [r["agent_id"] for r in all_rows]
        assert "agent_disabled" in all_ids

    def test_update_partial(self, repo):
        _make(repo, "agent_a", name="old", system_prompt="old prompt")
        repo.update(
            "agent_a",
            updated_at="2026-05-16T01:00:00Z",
            name="new",
            system_prompt="new prompt",
        )
        row = repo.get("agent_a")
        assert row["name"] == "new"
        assert row["system_prompt"] == "new prompt"
        # agent_mode 不变
        assert row["agent_mode"] == "quick"

    def test_update_builtin_prompt_allowed_at_repo_level(self, repo):
        """Repo 层允许改 builtin 的 prompt；is_builtin / agent_mode 由 API 层拦截。"""
        repo.update(
            "builtin-quick",
            updated_at="2026-05-16T01:00:00Z",
            system_prompt="用 markdown 嵌套粗体标题排版",
        )
        assert "markdown" in repo.get_builtin_quick()["system_prompt"]

    def test_soft_delete(self, repo):
        _make(repo, "agent_a")
        repo.soft_delete("agent_a", deleted_at="2026-05-16T01:00:00Z")
        assert repo.get("agent_a") is None
        row = repo.get("agent_a", include_deleted=True)
        assert row is not None
        assert row["deleted_at"] == "2026-05-16T01:00:00Z"

    def test_temperature_max_tokens_persist_correctly(self, repo):
        _make(repo, "agent_a", temperature=0.85)
        repo.update(
            "agent_a",
            updated_at="2026-05-16T01:00:00Z",
            temperature=0.15,
            max_tokens=2048,
        )
        row = repo.get("agent_a")
        assert abs(row["temperature"] - 0.15) < 1e-6
        assert row["max_tokens"] == 2048
