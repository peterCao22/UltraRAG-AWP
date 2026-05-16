"""Phase 7: ChatModelRepository 单测（SQLite 后端）。"""

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
    from custom_app.repositories import ChatModelRepository

    init_db()
    return ChatModelRepository()


def _make(repo, model_id: str, *, name: str = "M", provider: str = "gemini",
          is_default: bool = False, enabled: bool = True,
          extra: dict | None = None, api_key: str = "sk-xxx") -> None:
    repo.create(
        model_id=model_id, name=name, provider=provider,
        model_name="gemini-2.5-pro", base_url="", api_key=api_key,
        is_default=is_default, enabled=enabled,
        description="",
        extra=extra or {"temperature": 0.5},
        created_at="2026-05-15T00:00:00Z",
    )


class TestCRUD:
    def test_create_then_get(self, repo):
        _make(repo, "m1", name="Gemini Pro")
        row = repo.get("m1")
        assert row is not None
        assert row["name"] == "Gemini Pro"
        assert row["provider"] == "gemini"
        assert row["api_key"] == "sk-xxx"
        assert row["enabled"] is True
        assert row["is_default"] is False
        assert row["extra"] == {"temperature": 0.5}

    def test_list_active_excludes_disabled_by_default(self, repo):
        _make(repo, "m1", enabled=True)
        _make(repo, "m2", enabled=False)
        active = repo.list_active()
        ids = [r["model_id"] for r in active]
        assert ids == ["m1"]
        all_rows = repo.list_active(include_disabled=True)
        assert sorted(r["model_id"] for r in all_rows) == ["m1", "m2"]

    def test_update_partial(self, repo):
        _make(repo, "m1", name="old")
        repo.update("m1", updated_at="2026-05-15T01:00:00Z", name="new")
        row = repo.get("m1")
        assert row["name"] == "new"
        # 其它字段不动
        assert row["provider"] == "gemini"

    def test_update_api_key_only(self, repo):
        _make(repo, "m1", api_key="sk-old")
        repo.update("m1", updated_at="2026-05-15T01:00:00Z", api_key="sk-new")
        assert repo.get("m1")["api_key"] == "sk-new"

    def test_soft_delete(self, repo):
        _make(repo, "m1")
        repo.soft_delete("m1", deleted_at="2026-05-15T01:00:00Z")
        assert repo.get("m1") is None  # 默认排除已删
        row = repo.get("m1", include_deleted=True)
        assert row is not None
        assert row["deleted_at"] == "2026-05-15T01:00:00Z"


class TestSetDefault:
    def test_only_one_default_per_tenant(self, repo):
        _make(repo, "m1", is_default=True)
        _make(repo, "m2", is_default=False)
        repo.set_default("m2", updated_at="2026-05-15T01:00:00Z")
        assert repo.get("m1")["is_default"] is False
        assert repo.get("m2")["is_default"] is True

    def test_get_default_returns_marked(self, repo):
        _make(repo, "m1")
        _make(repo, "m2", is_default=True)
        d = repo.get_default()
        assert d["model_id"] == "m2"

    def test_get_default_falls_back_to_oldest_enabled(self, repo):
        # 无 is_default 标记时取最早创建且 enabled 的
        _make(repo, "m1")
        _make(repo, "m2")
        d = repo.get_default()
        assert d is not None
        assert d["enabled"] is True

    def test_get_default_skips_disabled(self, repo):
        _make(repo, "m1", enabled=False, is_default=True)
        _make(repo, "m2", enabled=True)
        d = repo.get_default()
        assert d["model_id"] == "m2"

    def test_get_default_returns_none_when_no_models(self, repo):
        assert repo.get_default() is None
