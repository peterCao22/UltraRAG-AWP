"""Phase 1：会话 REST 与落库契约。"""

import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    (tmp_path / "data" / "kb").mkdir(parents=True)
    yield tmp_path


@pytest.fixture()
def client(isolated_env):
    from custom_app.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_create_list_sessions(client):
    r = client.post("/api/sessions", json={"kb_id": "kb1", "agent_mode": "agent"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["success"] is True
    sid = d["data"]["session_id"]
    assert sid.startswith("sess_")

    r2 = client.get("/api/sessions", query_string={"kb_id": "kb1"})
    assert r2.status_code == 200
    items = r2.get_json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["session_id"] == sid


def test_get_session_and_messages_empty(client):
    r = client.post("/api/sessions", json={"kb_id": "kb2"})
    sid = r.get_json()["data"]["session_id"]
    r3 = client.get(f"/api/sessions/{sid}")
    assert r3.status_code == 200
    assert r3.get_json()["data"]["kb_id"] == "kb2"

    r4 = client.get(f"/api/sessions/{sid}/messages")
    assert r4.status_code == 200
    assert r4.get_json()["data"]["items"] == []


def test_patch_title(client):
    r = client.post("/api/sessions", json={"kb_id": "kb3"})
    sid = r.get_json()["data"]["session_id"]
    r2 = client.patch(f"/api/sessions/{sid}", json={"title": "自定义标题"})
    assert r2.status_code == 200
    assert r2.get_json()["data"]["title"] == "自定义标题"


def test_list_sessions_requires_kb_id(client):
    r = client.get("/api/sessions")
    assert r.status_code == 400


def test_stream_persists_messages_when_session_id(client, monkeypatch):
    """流式正常结束后应把 user/assistant 写入 kb_session_messages。"""
    import custom_app.api.chat as chat_mod

    r_sess = client.post("/api/sessions", json={"kb_id": "kb_stream"})
    sid = r_sess.get_json()["data"]["session_id"]

    class FakeRagRunner:
        def __init__(self, kb_id="agv_demo", **kwargs):
            self.kb_id = kb_id

        def init(self):
            return None

        def chat_stream(self, question, top_k=None, **kwargs):
            yield {"type": "chunk", "content": "hello"}
            yield {"type": "meta", "kb_id": self.kb_id, "meta": {}}
            yield {"type": "done", "answer": "hello"}

    monkeypatch.setattr(chat_mod, "RagRunner", FakeRagRunner)
    chat_mod._runners.clear()

    resp = client.post(
        "/api/chat/stream",
        json={"kb_id": "kb_stream", "question": "q1", "session_id": sid},
    )
    assert resp.status_code == 200
    stream_text = resp.get_data(as_text=True)
    assert '"type": "error"' not in stream_text, stream_text
    r_msgs = client.get(f"/api/sessions/{sid}/messages")
    items = r_msgs.get_json()["data"]["items"]
    assert len(items) == 2
    assert items[0]["role"] == "user" and items[0]["content"] == "q1"
    assert items[1]["role"] == "assistant" and items[1]["content"] == "hello"
