"""
Phase 2 KB API 单元测试
========================
覆盖 v1 修复项与新增功能：
- init_db 仅在启动时调用
- 绝对路径解析
- PUT 更新接口
- 文件上传接口
- 列表分页
- RBAC 角色与权限

运行：
  cd d:/Peter2025/myCursor/UltraRAG
  pytest tests/test_phase2_kb_api.py -v
"""

import io
import os
import sqlite3
import threading
import time
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import pytest


# ── 用 tempdir 隔离测试的 DB 与文件系统 ──────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """每个测试使用独立的 tmp_path 作为工作目录，避免互相污染。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    (tmp_path / "data" / "kb").mkdir(parents=True)
    # Phase 5.1.7: 重置 Repository default provider，避免跨测试单例污染
    from custom_app.repositories import set_default_provider
    set_default_provider(None)
    monkeypatch.setenv("ULTRARAG_DB_BACKEND", "sqlite")
    yield tmp_path
    set_default_provider(None)


@pytest.fixture()
def app(isolated_env):
    """创建 Flask 测试 app（mock 掉 init_db 不需要真实 embedding）。"""
    from custom_app.app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


# ─────────────────────────────────────────────────────────────────────────────
# T1: init_db 仅在 create_app 时调用，handler 不重复调用
# ─────────────────────────────────────────────────────────────────────────────

class TestInitDbCalledOnce:
    def test_init_db_called_on_app_creation(self, isolated_env):
        """create_app() 应调用 init_db()，DB 文件应已存在。"""
        from custom_app.app import create_app
        create_app()
        db_path = isolated_env / "db" / "app.sqlite"
        assert db_path.exists(), "app 启动后 db/app.sqlite 应已创建"

    def test_init_db_not_called_per_request(self, client, monkeypatch):
        """每次请求不应重复执行 CREATE TABLE DDL。"""
        call_count = {"n": 0}
        original_init = None

        from custom_app import db as db_mod
        original_init = db_mod.init_db

        def counting_init():
            call_count["n"] += 1
            original_init()

        monkeypatch.setattr(db_mod, "init_db", counting_init)

        # 发三次请求
        client.get("/api/kb")
        client.get("/api/kb")
        client.get("/api/kb")

        assert call_count["n"] == 0, (
            f"init_db 不应在 handler 中被调用，但被调用了 {call_count['n']} 次"
        )


# ─────────────────────────────────────────────────────────────────────────────
# T2: 路径使用绝对路径
# ─────────────────────────────────────────────────────────────────────────────

class TestAbsolutePaths:
    def test_create_kb_uses_absolute_data_path(self, client):
        """创建知识库时，data_path 应为绝对路径。"""
        resp = client.post("/api/kb", json={
            "kb_id": "test_abs",
            "name": "Test Absolute Path",
        })
        assert resp.status_code == 200
        # 查询详情验证路径
        resp2 = client.get("/api/kb/test_abs")
        data = resp2.get_json()["data"]
        assert Path(data["data_path"]).is_absolute(), (
            f"data_path 应为绝对路径，实际为: {data['data_path']}"
        )
        assert Path(data["index_path"]).is_absolute()
        assert Path(data["embedding_path"]).is_absolute()

    def test_kb_directories_created_at_absolute_path(self, client, isolated_env):
        """创建知识库后，raw/ 等子目录应在绝对路径下存在。"""
        client.post("/api/kb", json={"kb_id": "test_dir", "name": "Dir Test"})
        kb_root = isolated_env / "data" / "kb" / "test_dir"
        assert (kb_root / "raw").exists()
        assert (kb_root / "corpora").exists()
        assert (kb_root / "index").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T3: PUT /api/kb/<kb_id> 更新接口
# ─────────────────────────────────────────────────────────────────────────────

class TestUpdateKb:
    def _create(self, client, kb_id="kb_update_test"):
        client.post("/api/kb", json={"kb_id": kb_id, "name": "Original", "description": "old"})
        return kb_id

    def test_put_kb_updates_name(self, client):
        kb_id = self._create(client)
        resp = client.put(f"/api/kb/{kb_id}", json={"name": "Updated Name"})
        assert resp.status_code == 200
        detail = client.get(f"/api/kb/{kb_id}").get_json()["data"]
        assert detail["name"] == "Updated Name"

    def test_put_kb_updates_description(self, client):
        kb_id = self._create(client)
        resp = client.put(f"/api/kb/{kb_id}", json={"description": "new description"})
        assert resp.status_code == 200
        detail = client.get(f"/api/kb/{kb_id}").get_json()["data"]
        assert detail["description"] == "new description"

    def test_put_kb_not_found(self, client):
        resp = client.put("/api/kb/nonexistent", json={"name": "X"})
        assert resp.status_code == 404
        assert resp.get_json()["code"] == "KB_NOT_FOUND"

    def test_put_kb_empty_name_rejected(self, client):
        kb_id = self._create(client)
        resp = client.put(f"/api/kb/{kb_id}", json={"name": "  "})
        assert resp.status_code == 400

    def test_put_kb_partial_update(self, client):
        """只传 description，name 不变。"""
        kb_id = self._create(client)
        client.put(f"/api/kb/{kb_id}", json={"description": "new desc"})
        detail = client.get(f"/api/kb/{kb_id}").get_json()["data"]
        assert detail["name"] == "Original"
        assert detail["description"] == "new desc"


# ─────────────────────────────────────────────────────────────────────────────
# T4: POST /api/kb/<kb_id>/documents/upload 文件上传
# ─────────────────────────────────────────────────────────────────────────────

class TestDocumentUpload:
    def _create_kb(self, client, kb_id="kb_upload"):
        client.post("/api/kb", json={"kb_id": kb_id, "name": "Upload Test"})
        return kb_id

    def test_upload_single_docx(self, client, isolated_env):
        kb_id = self._create_kb(client)
        fake_docx = b"PK\x03\x04" + b"\x00" * 100  # minimal DOCX-like bytes
        data = {"file": (io.BytesIO(fake_docx), "test_doc.docx")}
        resp = client.post(
            f"/api/kb/{kb_id}/documents/upload",
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["uploaded"] == 1

    def test_upload_saves_to_raw_dir(self, client, isolated_env):
        kb_id = self._create_kb(client, "kb_upload2")
        fake_docx = b"PK\x03\x04" + b"\x00" * 100
        data = {"file": (io.BytesIO(fake_docx), "manual.docx")}
        client.post(
            f"/api/kb/{kb_id}/documents/upload",
            data=data,
            content_type="multipart/form-data",
        )
        raw_dir = isolated_env / "data" / "kb" / kb_id / "raw"
        assert (raw_dir / "manual.docx").exists()

    def test_upload_registers_document_record(self, client, isolated_env):
        kb_id = self._create_kb(client, "kb_upload3")
        fake_docx = b"PK\x03\x04" + b"\x00" * 100
        data = {"file": (io.BytesIO(fake_docx), "reg.docx")}
        client.post(
            f"/api/kb/{kb_id}/documents/upload",
            data=data,
            content_type="multipart/form-data",
        )
        docs_resp = client.get(f"/api/kb/{kb_id}/documents")
        docs = docs_resp.get_json()["data"]
        assert any(d["file_name"] == "reg.docx" for d in docs)

    def test_upload_to_nonexistent_kb(self, client):
        data = {"file": (io.BytesIO(b"fake"), "x.docx")}
        resp = client.post(
            "/api/kb/nonexistent/documents/upload",
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 404

    def test_upload_no_file_returns_400(self, client):
        kb_id = self._create_kb(client, "kb_upload4")
        resp = client.post(
            f"/api/kb/{kb_id}/documents/upload",
            data={},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_upload_multiple_files(self, client, isolated_env):
        kb_id = self._create_kb(client, "kb_upload5")
        fake = b"PK\x03\x04" + b"\x00" * 50
        # Flask test client 多文件同字段用 dict with list value
        from werkzeug.datastructures import MultiDict
        data = MultiDict([
            ("files", (io.BytesIO(fake), "doc1.docx")),
            ("files", (io.BytesIO(fake), "doc2.docx")),
        ])
        resp = client.post(
            f"/api/kb/{kb_id}/documents/upload",
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        assert resp.get_json()["data"]["uploaded"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# T5: 列表接口分页
# ─────────────────────────────────────────────────────────────────────────────

class TestPagination:
    def _seed_kbs(self, client, n=5):
        for i in range(n):
            client.post("/api/kb", json={"kb_id": f"kb_page_{i}", "name": f"KB {i}"})

    def test_list_kb_default_limit(self, client):
        self._seed_kbs(client, 5)
        resp = client.get("/api/kb")
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert isinstance(data, list)

    def test_list_kb_with_limit(self, client):
        self._seed_kbs(client, 5)
        resp = client.get("/api/kb?limit=2&offset=0")
        data = resp.get_json()["data"]
        assert len(data) == 2

    def test_list_kb_with_offset(self, client):
        self._seed_kbs(client, 5)
        resp_all = client.get("/api/kb?limit=5&offset=0").get_json()["data"]
        resp_offset = client.get("/api/kb?limit=5&offset=2").get_json()["data"]
        assert len(resp_offset) == 3
        assert resp_offset[0]["kb_id"] == resp_all[2]["kb_id"]

    def test_list_jobs_pagination(self, client, monkeypatch):
        """jobs 列表支持分页。"""
        kb_id = "kb_jobs_page"
        client.post("/api/kb", json={"kb_id": kb_id, "name": "Job Page KB"})
        # 直接插入若干 job 记录
        from custom_app.db import get_conn, now_iso, new_id
        with get_conn() as conn:
            for _ in range(4):
                jid = new_id("job")
                now = now_iso()
                conn.execute(
                    """INSERT INTO kb_jobs
                       (job_id, tenant_id, kb_id, job_type, status, payload_json, result_json, created_at, updated_at)
                       VALUES (?, 'default', ?, 'ingest', 'success', '{}', '{}', ?, ?)""",
                    (jid, kb_id, now, now),
                )
        resp = client.get(f"/api/kb/{kb_id}/jobs?limit=2&offset=0")
        assert resp.status_code == 200
        assert len(resp.get_json()["data"]) == 2

    def test_list_documents_pagination(self, client):
        kb_id = "kb_docs_page"
        client.post("/api/kb", json={"kb_id": kb_id, "name": "Docs Page KB"})
        from custom_app.db import get_conn, now_iso, new_id
        with get_conn() as conn:
            for i in range(4):
                did = new_id("doc")
                now = now_iso()
                conn.execute(
                    """INSERT INTO kb_documents
                       (kb_id, tenant_id, doc_id, file_name, file_type, file_path, channel, status, error_message, created_at, updated_at)
                       VALUES (?, 'default', ?, ?, 'docx', '/tmp/x.docx', 'api', 'indexed', '', ?, ?)""",
                    (kb_id, did, f"doc_{i}.docx", now, now),
                )
        resp = client.get(f"/api/kb/{kb_id}/documents?limit=2")
        assert resp.status_code == 200
        assert len(resp.get_json()["data"]) == 2


class TestKbDocumentCountAndDelete:
    """知识库列表/详情的 document_count 与 DELETE 单条文档。"""

    def test_list_kb_includes_document_count(self, client):
        kb_id = "kb_doc_cnt_list"
        client.post("/api/kb", json={"kb_id": kb_id, "name": "Count KB"})
        data = {"files": (io.BytesIO(b"hello"), "a.docx")}
        up = client.post(f"/api/kb/{kb_id}/documents/upload", data=data, content_type="multipart/form-data")
        assert up.status_code == 200
        rows = client.get("/api/kb").get_json()["data"]
        row = next((r for r in rows if r["kb_id"] == kb_id), None)
        assert row is not None
        assert row.get("document_count") == 1

    def test_get_kb_includes_document_count(self, client):
        kb_id = "kb_doc_cnt_get"
        client.post("/api/kb", json={"kb_id": kb_id, "name": "Count KB 2"})
        data = {"files": (io.BytesIO(b"x"), "b.docx")}
        client.post(f"/api/kb/{kb_id}/documents/upload", data=data, content_type="multipart/form-data")
        row = client.get(f"/api/kb/{kb_id}").get_json()["data"]
        assert row.get("document_count") == 1

    def test_delete_document_removes_row_and_file(self, client, isolated_env):
        kb_id = "kb_del_one"
        client.post("/api/kb", json={"kb_id": kb_id, "name": "Del Doc KB"})
        data = {"files": (io.BytesIO(b"content"), "gone.docx")}
        client.post(f"/api/kb/{kb_id}/documents/upload", data=data, content_type="multipart/form-data")
        docs = client.get(f"/api/kb/{kb_id}/documents").get_json()["data"]
        assert len(docs) == 1
        doc_id = docs[0]["doc_id"]
        raw_file = Path(docs[0]["file_path"])
        assert raw_file.exists()

        q = quote(doc_id, safe="")
        resp = client.delete(f"/api/kb/{kb_id}/documents?doc_id={q}")
        assert resp.status_code == 200
        assert resp.get_json()["data"]["deleted"] is True

        docs2 = client.get(f"/api/kb/{kb_id}/documents").get_json()["data"]
        assert docs2 == []
        assert not raw_file.exists()

    def test_delete_document_requires_doc_id(self, client):
        kb_id = "kb_del_bad"
        client.post("/api/kb", json={"kb_id": kb_id, "name": "x"})
        resp = client.delete(f"/api/kb/{kb_id}/documents")
        assert resp.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# T6: RBAC — 角色与知识库权限
# ─────────────────────────────────────────────────────────────────────────────

class TestRbac:
    def test_create_role(self, client):
        resp = client.post("/api/roles", json={"name": "editor", "description": "Can edit KBs"})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["name"] == "editor"
        assert "role_id" in data

    def test_list_roles(self, client):
        client.post("/api/roles", json={"name": "viewer"})
        client.post("/api/roles", json={"name": "admin"})
        resp = client.get("/api/roles")
        assert resp.status_code == 200
        roles = resp.get_json()["data"]
        names = [r["name"] for r in roles]
        assert "viewer" in names
        assert "admin" in names

    def test_get_role(self, client):
        r = client.post("/api/roles", json={"name": "ops"}).get_json()["data"]
        role_id = r["role_id"]
        resp = client.get(f"/api/roles/{role_id}")
        assert resp.status_code == 200
        assert resp.get_json()["data"]["name"] == "ops"

    def test_delete_role(self, client):
        r = client.post("/api/roles", json={"name": "temp"}).get_json()["data"]
        role_id = r["role_id"]
        resp = client.delete(f"/api/roles/{role_id}")
        assert resp.status_code == 200
        resp2 = client.get(f"/api/roles/{role_id}")
        assert resp2.status_code == 404

    def test_create_role_name_required(self, client):
        resp = client.post("/api/roles", json={"description": "no name"})
        assert resp.status_code == 400

    def test_assign_kb_to_role(self, client):
        """将知识库权限授予角色。"""
        client.post("/api/kb", json={"kb_id": "kb_rbac", "name": "RBAC KB"})
        r = client.post("/api/roles", json={"name": "rbac_role"}).get_json()["data"]
        role_id = r["role_id"]

        resp = client.post(f"/api/roles/{role_id}/permissions", json={
            "kb_id": "kb_rbac",
            "access_level": "read",
        })
        assert resp.status_code == 200

    def test_list_role_permissions(self, client):
        """列出角色拥有的知识库权限。"""
        client.post("/api/kb", json={"kb_id": "kb_rbac2", "name": "RBAC KB2"})
        client.post("/api/kb", json={"kb_id": "kb_rbac3", "name": "RBAC KB3"})
        r = client.post("/api/roles", json={"name": "power_user"}).get_json()["data"]
        role_id = r["role_id"]
        client.post(f"/api/roles/{role_id}/permissions", json={"kb_id": "kb_rbac2", "access_level": "read"})
        client.post(f"/api/roles/{role_id}/permissions", json={"kb_id": "kb_rbac3", "access_level": "write"})

        resp = client.get(f"/api/roles/{role_id}/permissions")
        assert resp.status_code == 200
        perms = resp.get_json()["data"]
        kb_ids = [p["kb_id"] for p in perms]
        assert "kb_rbac2" in kb_ids
        assert "kb_rbac3" in kb_ids

    def test_revoke_kb_from_role(self, client):
        client.post("/api/kb", json={"kb_id": "kb_rbac4", "name": "Revoke KB"})
        r = client.post("/api/roles", json={"name": "temp_role"}).get_json()["data"]
        role_id = r["role_id"]
        client.post(f"/api/roles/{role_id}/permissions", json={"kb_id": "kb_rbac4", "access_level": "read"})

        resp = client.delete(f"/api/roles/{role_id}/permissions/kb_rbac4")
        assert resp.status_code == 200

        perms = client.get(f"/api/roles/{role_id}/permissions").get_json()["data"]
        assert not any(p["kb_id"] == "kb_rbac4" for p in perms)

    def test_list_kb_filtered_by_role(self, client):
        """GET /api/kb?role_id=xxx 只返回该角色有权限的知识库。"""
        client.post("/api/kb", json={"kb_id": "kb_in_role", "name": "In Role"})
        client.post("/api/kb", json={"kb_id": "kb_not_in_role", "name": "Not in Role"})
        r = client.post("/api/roles", json={"name": "limited"}).get_json()["data"]
        role_id = r["role_id"]
        client.post(f"/api/roles/{role_id}/permissions", json={"kb_id": "kb_in_role", "access_level": "read"})

        resp = client.get(f"/api/kb?role_id={role_id}")
        assert resp.status_code == 200
        kb_ids = [kb["kb_id"] for kb in resp.get_json()["data"]]
        assert "kb_in_role" in kb_ids
        assert "kb_not_in_role" not in kb_ids

    def test_duplicate_permission_returns_ok(self, client):
        """重复授权同一个 kb 给同一个 role 应该是幂等的。"""
        client.post("/api/kb", json={"kb_id": "kb_dup", "name": "Dup KB"})
        r = client.post("/api/roles", json={"name": "dup_role"}).get_json()["data"]
        role_id = r["role_id"]
        client.post(f"/api/roles/{role_id}/permissions", json={"kb_id": "kb_dup", "access_level": "read"})
        resp = client.post(f"/api/roles/{role_id}/permissions", json={"kb_id": "kb_dup", "access_level": "write"})
        assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# T7: chat runner 线程安全
# ─────────────────────────────────────────────────────────────────────────────

class TestChatRunnerThreadSafety:
    def test_concurrent_kb_switch_no_race(self, client, monkeypatch):
        """并发请求使用不同 kb_id 时不应出现竞态导致的错误响应。"""
        import custom_app.api.chat as chat_mod

        init_calls = []
        lock = threading.Lock()

        class FakeRagRunner:
            def __init__(self, kb_id="agv_demo", **kwargs):
                self.kb_id = kb_id

            def init(self):
                with lock:
                    init_calls.append(self.kb_id)
                time.sleep(0.05)  # simulate slow init

            def chat(self, question, top_k=None):
                return {"answer": f"ok from {self.kb_id}", "answer_blocks": [], "sources": []}

        # 清除 runner 缓存，确保 monkeypatch 生效
        monkeypatch.setattr(chat_mod, "RagRunner", FakeRagRunner)
        monkeypatch.setitem(chat_mod._runners, "agv_demo", None)
        chat_mod._runners.clear()

        errors = []
        results = []

        def ask(kb_id):
            try:
                r = client.post("/api/chat", json={"kb_id": kb_id, "question": "test"})
                results.append(r.status_code)
            except Exception as e:
                errors.append(str(e))

        threads = [
            threading.Thread(target=ask, args=(f"kb_{i % 3}",))
            for i in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"并发请求出现异常: {errors}"
        assert all(s == 200 for s in results), f"部分请求失败: {results}"


# ─────────────────────────────────────────────────────────────────────────────
# T8: 分块预览 GET /api/kb/<kb_id>/chunks
# ─────────────────────────────────────────────────────────────────────────────

class TestChunksPreview:
    def _create_kb(self, client, kb_id="kb_chunks"):
        client.post("/api/kb", json={"kb_id": kb_id, "name": f"KB {kb_id}"})
        return kb_id

    def _write_chunks_jsonl(self, isolated_env, kb_id, chunks):
        """在 tmp_path 下写入 chunks.jsonl 文件供接口读取。"""
        corpora_dir = isolated_env / "data" / "kb" / kb_id / "corpora"
        corpora_dir.mkdir(parents=True, exist_ok=True)
        chunks_path = corpora_dir / "chunks.jsonl"
        lines = [
            __import__("json").dumps(c, ensure_ascii=False) for c in chunks
        ]
        chunks_path.write_text("\n".join(lines), encoding="utf-8")
        return chunks_path

    def test_chunks_empty_when_no_file(self, client, isolated_env):
        """chunks.jsonl 不存在时返回空列表。"""
        kb_id = self._create_kb(client, "kb_no_chunks")
        resp = client.get(f"/api/kb/{kb_id}/chunks")
        assert resp.status_code == 200
        assert resp.get_json()["data"] == []

    def test_chunks_404_when_kb_not_found(self, client):
        resp = client.get("/api/kb/nonexistent_kb/chunks")
        assert resp.status_code == 404

    def test_chunks_returns_preview(self, client, isolated_env):
        """chunks.jsonl 存在时返回包含 preview 字段的列表。"""
        kb_id = self._create_kb(client, "kb_with_chunks")
        self._write_chunks_jsonl(isolated_env, kb_id, [
            {"id": "c1", "title": "Intro", "contents": "This is intro content.", "doc": "manual.docx", "images": []},
            {"id": "c2", "title": "STEP 1", "contents": "Step one details here.", "doc": "manual.docx", "images": ["img1.png"]},
        ])
        resp = client.get(f"/api/kb/{kb_id}/chunks")
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert len(data) == 2
        assert data[0]["id"] == "c1"
        assert data[0]["title"] == "Intro"
        assert "preview" in data[0]
        assert data[0]["image_count"] == 0
        assert data[1]["image_count"] == 1

    def test_chunks_preview_truncated(self, client, isolated_env):
        """contents 超过 max_chars 时应被截断。"""
        kb_id = self._create_kb(client, "kb_trunc")
        long_text = "A" * 1000
        self._write_chunks_jsonl(isolated_env, kb_id, [
            {"id": "c1", "title": "Long", "contents": long_text, "doc": "d.docx", "images": []}
        ])
        resp = client.get(f"/api/kb/{kb_id}/chunks?max_chars=100")
        data = resp.get_json()["data"]
        assert len(data[0]["preview"]) <= 103  # 100 + possible "..."

    def test_chunks_limit_and_offset(self, client, isolated_env):
        """支持 limit/offset 分页。"""
        kb_id = self._create_kb(client, "kb_page_chunks")
        chunks = [
            {"id": f"c{i}", "title": f"T{i}", "contents": f"content {i}", "doc": "d.docx", "images": []}
            for i in range(10)
        ]
        self._write_chunks_jsonl(isolated_env, kb_id, chunks)
        resp = client.get(f"/api/kb/{kb_id}/chunks?limit=3&offset=2")
        data = resp.get_json()["data"]
        assert len(data) == 3
        assert data[0]["id"] == "c2"

    def test_chunks_filter_by_doc(self, client, isolated_env):
        """?doc=xxx 只返回匹配文档的分块。"""
        kb_id = self._create_kb(client, "kb_doc_filter")
        self._write_chunks_jsonl(isolated_env, kb_id, [
            {"id": "c1", "title": "A", "contents": "aa", "doc": "doc_a.docx", "images": []},
            {"id": "c2", "title": "B", "contents": "bb", "doc": "doc_b.docx", "images": []},
            {"id": "c3", "title": "A2", "contents": "aa2", "doc": "doc_a.docx", "images": []},
        ])
        resp = client.get(f"/api/kb/{kb_id}/chunks?doc=doc_a.docx")
        data = resp.get_json()["data"]
        assert len(data) == 2
        assert all(c["doc"] == "doc_a.docx" for c in data)


# ─────────────────────────────────────────────────────────────────────────────
# T9: Ingest 进度 GET /api/kb/<kb_id>/jobs/<job_id>/progress
# ─────────────────────────────────────────────────────────────────────────────

class TestJobProgress:
    def _create_kb(self, client, kb_id="kb_prog"):
        client.post("/api/kb", json={"kb_id": kb_id, "name": f"KB {kb_id}"})
        return kb_id

    def _insert_job(self, kb_id, status="success", result=None):
        import json as _json
        from custom_app.db import get_conn, now_iso, new_id
        jid = new_id("job")
        now = now_iso()
        result_json = _json.dumps(result or {})
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO kb_jobs
                   (job_id, tenant_id, kb_id, job_type, status, payload_json, result_json, created_at, updated_at)
                   VALUES (?, 'default', ?, 'ingest', ?, '{}', ?, ?, ?)""",
                (jid, kb_id, status, result_json, now, now),
            )
        return jid

    def test_progress_404_when_kb_not_found(self, client):
        resp = client.get("/api/kb/no_kb/jobs/no_job/progress")
        assert resp.status_code == 404

    def test_progress_404_when_job_not_found(self, client):
        kb_id = self._create_kb(client, "kb_prog1")
        resp = client.get(f"/api/kb/{kb_id}/jobs/nonexistent_job/progress")
        assert resp.status_code == 404

    def test_progress_success_job(self, client):
        """成功任务返回 stage=done 和 chunk_count。"""
        kb_id = self._create_kb(client, "kb_prog2")
        jid = self._insert_job(kb_id, status="success", result={
            "chunk_count": 42,
            "stages_done": ["parse", "embed", "index"],
        })
        resp = client.get(f"/api/kb/{kb_id}/jobs/{jid}/progress")
        assert resp.status_code == 200
        d = resp.get_json()["data"]
        assert d["job_id"] == jid
        assert d["status"] == "success"
        assert d["chunk_count"] == 42
        assert d["stage"] == "done"
        assert "parse" in d["stages_done"]

    def test_progress_running_job_with_stage(self, client):
        """运行中的任务返回当前已完成的阶段。"""
        kb_id = self._create_kb(client, "kb_prog3")
        jid = self._insert_job(kb_id, status="running", result={
            "stages_done": ["parse"],
            "file_count": 3,
        })
        resp = client.get(f"/api/kb/{kb_id}/jobs/{jid}/progress")
        assert resp.status_code == 200
        d = resp.get_json()["data"]
        assert d["status"] == "running"
        assert d["stage"] == "parse"
        assert d["file_count"] == 3

    def test_progress_failed_job(self, client):
        """失败任务返回 error 字段。"""
        kb_id = self._create_kb(client, "kb_prog4")
        jid = self._insert_job(kb_id, status="failed", result={"stages_done": ["parse"]})
        from custom_app.db import get_conn, now_iso
        with get_conn() as conn:
            conn.execute(
                "UPDATE kb_jobs SET last_error='embed failed: OOM' WHERE job_id=?", (jid,)
            )
        resp = client.get(f"/api/kb/{kb_id}/jobs/{jid}/progress")
        assert resp.status_code == 200
        d = resp.get_json()["data"]
        assert d["status"] == "failed"
        assert "embed failed" in d.get("error", "")


# ─────────────────────────────────────────────────────────────────────────────
# T9: /api/chat/stream SSE（RagRunner.chat_stream）
# ─────────────────────────────────────────────────────────────────────────────

def _parse_sse_events(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8")
    events = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[len("data: ") :].strip()
            if not payload or payload == "[DONE]":
                continue
            events.append(__import__("json").loads(payload))
    return events


class TestChatStreamSse:
    def test_stream_happy_path(self, client, monkeypatch):
        import custom_app.api.chat as chat_mod

        class FakeRagRunner:
            def __init__(self, kb_id="agv_demo", **kwargs):
                self.kb_id = kb_id

            def init(self):
                return None

            def chat_stream(self, question, top_k=None, **kwargs):
                profile = kwargs.get("profile", False)
                yield {"type": "status", "content": "busy"}
                yield {"type": "chunk", "content": f"echo:{question}"}
                yield {"type": "sources", "sources": [{"title": "S1"}]}
                meta = {"type": "meta", "kb_id": self.kb_id}
                if profile:
                    meta["phase_timings_ms"] = {"prepare_context_ms": 0.01}
                yield meta
                yield {"type": "done"}

        monkeypatch.setattr(chat_mod, "RagRunner", FakeRagRunner)
        chat_mod._runners.clear()

        resp = client.post(
            "/api/chat/stream",
            json={"kb_id": "kb_sse", "question": "hi"},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in (resp.mimetype or "")
        events = _parse_sse_events(resp.data)
        assert [e["type"] for e in events] == [
            "status",
            "status",
            "chunk",
            "sources",
            "meta",
            "done",
        ]
        assert "索引" in (events[0].get("content") or "")
        assert events[2]["content"] == "echo:hi"
        assert events[3]["sources"][0]["title"] == "S1"

    def test_stream_profile_json_passes_profile_to_runner(self, client, monkeypatch):
        import custom_app.api.chat as chat_mod

        seen: dict = {}

        class FakeRagRunner:
            def __init__(self, kb_id="agv_demo", **kwargs):
                self.kb_id = kb_id

            def init(self):
                return None

            def chat_stream(self, question, top_k=None, **kwargs):
                seen["profile"] = kwargs.get("profile", False)
                yield {"type": "meta", "kb_id": self.kb_id}
                yield {"type": "done"}

        monkeypatch.setattr(chat_mod, "RagRunner", FakeRagRunner)
        chat_mod._runners.clear()

        resp = client.post(
            "/api/chat/stream",
            json={"kb_id": "kb_prof", "question": "hi", "profile": True},
        )
        assert resp.status_code == 200
        _ = resp.get_data()
        assert seen.get("profile") is True

    def test_stream_passes_agent_mode_to_runner(self, client, monkeypatch):
        import custom_app.api.chat as chat_mod

        seen: dict = {}

        class FakeRagRunner:
            def __init__(self, kb_id="agv_demo", **kwargs):
                self.kb_id = kb_id

            def init(self):
                return None

            def chat_stream(self, question, top_k=None, **kwargs):
                seen["agent_mode"] = kwargs.get("agent_mode", "quick")
                yield {"type": "meta", "kb_id": self.kb_id}
                yield {"type": "done", "answer": ""}

        monkeypatch.setattr(chat_mod, "RagRunner", FakeRagRunner)
        chat_mod._runners.clear()

        resp = client.post(
            "/api/chat/stream",
            json={"kb_id": "kb_am", "question": "hi", "agent_mode": "agent"},
        )
        assert resp.status_code == 200
        _ = resp.get_data()
        assert seen.get("agent_mode") == "agent"

    def test_stream_empty_question_400(self, client):
        resp = client.post("/api/chat/stream", json={"kb_id": "x", "question": "  "})
        assert resp.status_code == 400

    def test_stream_exception_becomes_error_event(self, client, monkeypatch):
        import custom_app.api.chat as chat_mod

        class BrokenRagRunner:
            def __init__(self, kb_id="agv_demo", **kwargs):
                self.kb_id = kb_id

            def init(self):
                return None

            def chat_stream(self, question, top_k=None, **kwargs):
                if True:
                    raise RuntimeError("boom")
                yield {"type": "done"}  # pragma: no cover

        monkeypatch.setattr(chat_mod, "RagRunner", BrokenRagRunner)
        chat_mod._runners.clear()

        resp = client.post(
            "/api/chat/stream",
            json={"kb_id": "kb_err", "question": "q"},
        )
        assert resp.status_code == 200
        raw = resp.get_data()
        events = _parse_sse_events(raw)
        assert events[0]["type"] == "status"
        assert events[-1] == {"type": "error", "content": "boom"}
