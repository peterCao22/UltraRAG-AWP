"""Phase 7: /api/admin/models 路由单测（SQLite 后端 + Flask test client）。"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    (tmp_path / "data" / "kb").mkdir(parents=True)
    from custom_app.repositories import set_default_provider

    set_default_provider(None)
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")
    yield tmp_path
    set_default_provider(None)


@pytest.fixture
def client(isolated_env):
    from custom_app.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _create_payload(**overrides):
    base = {
        "name": "Gemini Pro",
        "provider": "gemini",
        "model_name": "gemini-2.5-pro",
        "base_url": "",
        "api_key": "sk-test",
        "description": "",
        "enabled": True,
        "is_default": False,
        "extra": {"temperature": 0.5},
    }
    base.update(overrides)
    return base


class TestProviderList:
    def test_returns_four_providers(self, client):
        resp = client.get("/api/admin/models/providers")
        assert resp.status_code == 200
        items = resp.get_json()["data"]
        names = {x["name"] for x in items}
        assert names == {"gemini", "openai", "anthropic", "openai_compatible"}


class TestCreateModel:
    def test_creates_and_returns_masked(self, client):
        resp = client.post("/api/admin/models", json=_create_payload())
        assert resp.status_code == 200
        row = resp.get_json()["data"]
        assert row["name"] == "Gemini Pro"
        assert row["api_key"] == "***"  # 屏蔽
        assert row["enabled"] is True
        assert row["is_default"] is False
        assert "id" not in row  # 内部主键不暴露
        assert row["model_id"].startswith("model_")

    def test_rejects_missing_fields(self, client):
        resp = client.post("/api/admin/models", json={"name": "x"})
        assert resp.status_code == 400

    def test_rejects_invalid_provider(self, client):
        resp = client.post(
            "/api/admin/models",
            json=_create_payload(provider="bogus"),
        )
        assert resp.status_code == 400

    def test_rejects_missing_api_key_for_auth_provider(self, client):
        resp = client.post(
            "/api/admin/models",
            json=_create_payload(api_key=""),
        )
        assert resp.status_code == 400

    def test_openai_compatible_allows_empty_api_key(self, client):
        resp = client.post(
            "/api/admin/models",
            json=_create_payload(
                provider="openai_compatible",
                model_name="Qwen2.5-7B",
                api_key="",
                base_url="http://192.168.8.40:8000/v1",
            ),
        )
        assert resp.status_code == 200

    def test_rejects_ssrf_url(self, client):
        resp = client.post(
            "/api/admin/models",
            json=_create_payload(base_url="ftp://example.com"),
        )
        assert resp.status_code == 400


class TestListAndGet:
    def test_list_returns_all_including_disabled(self, client):
        client.post("/api/admin/models", json=_create_payload(name="A"))
        client.post("/api/admin/models", json=_create_payload(name="B", enabled=False))
        resp = client.get("/api/admin/models")
        rows = resp.get_json()["data"]
        names = {r["name"] for r in rows}
        assert names == {"A", "B"}
        # api_key 都被屏蔽
        for r in rows:
            assert r["api_key"] == "***"

    def test_get_single(self, client):
        c = client.post("/api/admin/models", json=_create_payload(name="One"))
        mid = c.get_json()["data"]["model_id"]
        r = client.get(f"/api/admin/models/{mid}")
        assert r.status_code == 200
        assert r.get_json()["data"]["model_id"] == mid

    def test_get_404(self, client):
        r = client.get("/api/admin/models/nonexistent")
        assert r.status_code == 404


class TestUpdateModel:
    def test_partial_update(self, client):
        c = client.post("/api/admin/models", json=_create_payload(name="old"))
        mid = c.get_json()["data"]["model_id"]
        r = client.put(f"/api/admin/models/{mid}", json={"name": "new"})
        assert r.status_code == 200
        assert r.get_json()["data"]["name"] == "new"

    def test_empty_api_key_does_not_change(self, client):
        c = client.post("/api/admin/models", json=_create_payload(api_key="sk-old"))
        mid = c.get_json()["data"]["model_id"]
        # 传空字符串：不变
        r = client.put(f"/api/admin/models/{mid}", json={"api_key": ""})
        assert r.status_code == 200
        # 直接读数据库验证
        from custom_app.repositories import ChatModelRepository
        row = ChatModelRepository().get(mid)
        assert row["api_key"] == "sk-old"

    def test_non_empty_api_key_overrides(self, client):
        c = client.post("/api/admin/models", json=_create_payload(api_key="sk-old"))
        mid = c.get_json()["data"]["model_id"]
        client.put(f"/api/admin/models/{mid}", json={"api_key": "sk-new"})
        from custom_app.repositories import ChatModelRepository
        assert ChatModelRepository().get(mid)["api_key"] == "sk-new"


class TestDeleteModel:
    def test_soft_delete(self, client):
        c = client.post("/api/admin/models", json=_create_payload(name="gone"))
        mid = c.get_json()["data"]["model_id"]
        r = client.delete(f"/api/admin/models/{mid}")
        assert r.status_code == 200
        # 列表不再包含
        rows = client.get("/api/admin/models").get_json()["data"]
        assert all(x["model_id"] != mid for x in rows)


class TestSetDefault:
    def test_marks_only_one(self, client):
        a = client.post("/api/admin/models", json=_create_payload(name="A")).get_json()["data"]
        b = client.post("/api/admin/models", json=_create_payload(name="B")).get_json()["data"]
        client.post(f"/api/admin/models/{a['model_id']}/set-default")
        client.post(f"/api/admin/models/{b['model_id']}/set-default")
        from custom_app.repositories import ChatModelRepository
        repo = ChatModelRepository()
        assert repo.get(a["model_id"])["is_default"] is False
        assert repo.get(b["model_id"])["is_default"] is True


class TestTestConnection:
    def test_dispatches_to_provider_adapter(self, client):
        """Phase 7.1: gemini 走 OpenAICompatAdapter（Google OpenAI 兼容端点）。"""
        c = client.post("/api/admin/models", json=_create_payload(provider="gemini"))
        mid = c.get_json()["data"]["model_id"]
        # mock 掉 OpenAICompatAdapter.test_ping 避免真的网络调用
        with patch(
            "custom_app.services.providers.openai_compat_adapter.OpenAICompatAdapter.test_ping",
            return_value={"ok": True, "latency_ms": 123, "sample": "pong"},
        ):
            r = client.post(f"/api/admin/models/{mid}/test")
        assert r.status_code == 200
        data = r.get_json()["data"]
        assert data["ok"] is True
        assert data["latency_ms"] == 123
