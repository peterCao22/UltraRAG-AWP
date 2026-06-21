"""Phase 1：会话 REST 与落库契约。"""

import pytest

from custom_app.services.session_store import _make_session_title


# 测试用的 KB ID（与文件内 test_* 函数中硬编码的对应）
# 远程 Postgres 后端下文件系统隔离无效，需 teardown 显式清理
_TEST_KB_IDS = ("kb1", "kb2", "kb3", "kb_stream")


def _cleanup_test_kb_sessions() -> None:
    """删除测试用 kb_id 下的所有 session + messages（远程 DB 后端用）。

    本地 SQLite 走 isolated_env 的 chdir(tmp_path) 已天然隔离，此函数无效；
    远程 Postgres 必须显式清理避免测试间互相污染（本次 push 前发现 kb1
    残留导致 test_create_list_sessions 一直失败）。
    """
    try:
        from custom_app.repositories.base import get_default_provider
        provider = get_default_provider()
    except Exception:
        return  # 后端不可用就算了，不要让 teardown 拖垮测试
    placeholders = ", ".join(["%s" if provider.placeholder == "%s" else "?"] * len(_TEST_KB_IDS))
    try:
        with provider.connect() as conn:
            # 先删子表（messages），再删父表（sessions）
            conn.execute(
                f"DELETE FROM kb_session_messages WHERE session_id IN ("
                f"  SELECT session_id FROM kb_sessions WHERE kb_id IN ({placeholders})"
                f")",
                _TEST_KB_IDS,
            )
            conn.execute(
                f"DELETE FROM kb_sessions WHERE kb_id IN ({placeholders})",
                _TEST_KB_IDS,
            )
    except Exception:
        pass  # 清理失败不影响测试结果（最坏情况下次测试断言会发现）


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    (tmp_path / "data" / "kb").mkdir(parents=True)
    # P-Perm（Commit 4）全局登录中间件；旧 Phase 1 测试不携带登录态，关掉拦截。
    monkeypatch.setenv("ULTRARAG_AUTH_REQUIRED", "0")
    # setup: 先清理可能的历史残留
    _cleanup_test_kb_sessions()
    yield tmp_path
    # teardown: 测试结束后清理本次创建的
    _cleanup_test_kb_sessions()


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


def test_make_session_title_compacts_long_english_question():
    title = _make_session_title(
        "What alarm ID is triggered when there is an obstruction in the AGV right arm?"
    )
    assert title == "Right arm obstruction alarm ID"
    assert len(title) < 48


def test_make_session_title_compacts_chinese_question():
    title = _make_session_title("将电池滑出后，下一步需要注意什么？")
    assert title == "将电池滑出后，下一步注意事项"


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
