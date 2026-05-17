"""Phase 7.2.A: /api/admin/agents 路由单测（SQLite + Flask test client）。"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

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
        "name": "商业资料助手",
        "agent_mode": "quick",
        "description": "测试用",
        "system_prompt": "你是一个商业资料助手。",
        "agent_system_prompt": "",
        "model_id": "",
        "temperature": 0.4,
        "max_tokens": 2048,
        "enabled": True,
    }
    base.update(overrides)
    return base


class TestList:
    def test_list_includes_two_builtins(self, client):
        resp = client.get("/api/admin/agents")
        assert resp.status_code == 200
        rows = resp.get_json()["data"]
        agent_ids = {r["agent_id"] for r in rows}
        assert "builtin-quick" in agent_ids
        assert "builtin-agent" in agent_ids
        # builtin 排序在前
        assert rows[0]["is_builtin"] is True

    def test_list_returns_internal_id_stripped(self, client):
        resp = client.get("/api/admin/agents")
        for row in resp.get_json()["data"]:
            assert "id" not in row


class TestCreate:
    def test_creates_user_agent(self, client):
        resp = client.post("/api/admin/agents", json=_create_payload())
        assert resp.status_code == 200, resp.get_json()
        row = resp.get_json()["data"]
        assert row["name"] == "商业资料助手"
        assert row["is_builtin"] is False
        assert row["agent_id"].startswith("agent_")
        assert "id" not in row

    def test_rejects_missing_name(self, client):
        resp = client.post(
            "/api/admin/agents", json=_create_payload(name="")
        )
        assert resp.status_code == 400

    def test_rejects_invalid_agent_mode(self, client):
        resp = client.post(
            "/api/admin/agents", json=_create_payload(agent_mode="bogus")
        )
        assert resp.status_code == 400

    def test_rejects_out_of_range_temperature(self, client):
        resp = client.post(
            "/api/admin/agents", json=_create_payload(temperature=3.5)
        )
        assert resp.status_code == 400


class TestUpdate:
    def test_update_user_agent(self, client):
        resp = client.post("/api/admin/agents", json=_create_payload())
        agent_id = resp.get_json()["data"]["agent_id"]

        resp = client.put(
            f"/api/admin/agents/{agent_id}",
            json={"name": "更新后", "system_prompt": "new prompt"},
        )
        assert resp.status_code == 200
        row = resp.get_json()["data"]
        assert row["name"] == "更新后"
        assert row["system_prompt"] == "new prompt"
        # agent_mode 不变
        assert row["agent_mode"] == "quick"

    def test_update_builtin_prompt_allowed(self, client):
        resp = client.put(
            "/api/admin/agents/builtin-quick",
            json={"system_prompt": "用 markdown 嵌套粗体标题排版。"},
        )
        assert resp.status_code == 200
        row = resp.get_json()["data"]
        assert "markdown" in row["system_prompt"]

    def test_update_rejects_agent_mode_change(self, client):
        resp = client.put(
            "/api/admin/agents/builtin-quick",
            json={"agent_mode": "agent"},
        )
        assert resp.status_code == 400

    def test_update_rejects_is_builtin_change(self, client):
        resp = client.put(
            "/api/admin/agents/builtin-quick",
            json={"is_builtin": False},
        )
        assert resp.status_code == 400

    def test_update_404_for_unknown_agent(self, client):
        resp = client.put(
            "/api/admin/agents/agent_nope",
            json={"name": "x"},
        )
        assert resp.status_code == 404


class TestDelete:
    def test_user_agent_can_be_deleted(self, client):
        resp = client.post("/api/admin/agents", json=_create_payload())
        agent_id = resp.get_json()["data"]["agent_id"]

        resp = client.delete(f"/api/admin/agents/{agent_id}")
        assert resp.status_code == 200
        assert resp.get_json()["data"]["deleted"] is True
        # 再 GET 应 404
        assert client.get(f"/api/admin/agents/{agent_id}").status_code == 404

    def test_builtin_cannot_be_deleted(self, client):
        resp = client.delete("/api/admin/agents/builtin-quick")
        assert resp.status_code == 400
        assert resp.get_json()["code"] == "BUILTIN_IMMUTABLE"
        # 仍存在
        assert client.get("/api/admin/agents/builtin-quick").status_code == 200

    def test_delete_404_for_unknown(self, client):
        resp = client.delete("/api/admin/agents/agent_nope")
        assert resp.status_code == 404


class TestChatAgentsEndpoint:
    def test_chat_agents_returns_minimal_fields(self, client):
        resp = client.get("/api/chat/agents")
        assert resp.status_code == 200
        rows = resp.get_json()["data"]
        ids = {r["agent_id"] for r in rows}
        assert "builtin-quick" in ids and "builtin-agent" in ids
        # 字段只暴露最小集
        for r in rows:
            assert set(r.keys()) <= {
                "agent_id", "name", "agent_mode", "avatar",
                "description", "is_builtin", "model_id",
            }
            assert "system_prompt" not in r
            assert "agent_system_prompt" not in r

    def test_chat_agents_skips_disabled(self, client):
        create_resp = client.post(
            "/api/admin/agents",
            json=_create_payload(name="禁用助手", enabled=False),
        )
        agent_id = create_resp.get_json()["data"]["agent_id"]

        resp = client.get("/api/chat/agents")
        ids = {r["agent_id"] for r in resp.get_json()["data"]}
        assert agent_id not in ids
