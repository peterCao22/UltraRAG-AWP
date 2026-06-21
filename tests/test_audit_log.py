"""Phase 11.1.2（最小版）审计日志单元测试。

覆盖：
  1. log_qa 成功写入：传 chunk_ids/extra_meta 时 JSON 化正确
  2. log_qa Repository 抛异常 → 静默降级返回 False，不抛回主链路
  3. session_id None → 落库为 ""，不报错
  4. AuditRepository.list_recent 按 kb_id/session_id 过滤
  5. count 同步过滤
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from custom_app.services import audit_log


# ---------------------------------------------------------------------------
# log_qa
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_repo(monkeypatch):
    """替换 AuditRepository 为 MagicMock。"""
    instance = MagicMock()
    monkeypatch.setattr(
        "custom_app.services.audit_log.AuditRepository",
        lambda *a, **kw: instance,
    )
    return instance


def test_log_qa_success(fake_repo):
    ok = audit_log.log_qa(
        kb_id="agv_demo",
        session_id="sess_xx",
        query="如何检查急停按钮",
        answer="检查按下/弹起 + 切电测试",
        chunk_ids=["BatteryChange_step_1", "BatteryChange_step_2"],
        extra_meta={"agent_mode": "quick", "retrieval_source_count": 5},
    )

    assert ok is True
    fake_repo.append.assert_called_once()
    kwargs = fake_repo.append.call_args.kwargs
    assert kwargs["kb_id"] == "agv_demo"
    assert kwargs["session_id"] == "sess_xx"
    assert kwargs["event_type"] == "qa"
    assert kwargs["query"] == "如何检查急停按钮"
    assert kwargs["answer"] == "检查按下/弹起 + 切电测试"
    chunks = json.loads(kwargs["chunk_ids_json"])
    assert chunks == ["BatteryChange_step_1", "BatteryChange_step_2"]
    meta = json.loads(kwargs["meta_json"])
    assert meta["agent_mode"] == "quick"
    assert meta["retrieval_source_count"] == 5
    # ts 必须是非空字符串
    assert isinstance(kwargs["ts"], str) and kwargs["ts"]


def test_log_qa_repository_failure_swallows(fake_repo):
    """Repository 抛异常 → 返回 False，不抛回主链路。"""
    fake_repo.append.side_effect = RuntimeError("DB down")

    ok = audit_log.log_qa(
        kb_id="agv_demo",
        session_id="sess_xx",
        query="q",
        answer="a",
    )

    assert ok is False  # 静默降级


def test_log_qa_none_session_id_becomes_empty_string(fake_repo):
    """匿名场景：session_id=None 落库为 ''。"""
    audit_log.log_qa(
        kb_id="agv_demo",
        session_id=None,
        query="q",
        answer="a",
    )
    kwargs = fake_repo.append.call_args.kwargs
    assert kwargs["session_id"] == ""


def test_log_qa_defaults_empty_collections(fake_repo):
    """chunk_ids / extra_meta 为 None 时序列化为空集合。"""
    audit_log.log_qa(
        kb_id="agv_demo",
        session_id="sess_xx",
        query="q",
        answer="a",
        chunk_ids=None,
        extra_meta=None,
    )
    kwargs = fake_repo.append.call_args.kwargs
    assert json.loads(kwargs["chunk_ids_json"]) == []
    assert json.loads(kwargs["meta_json"]) == {}


def test_log_qa_preserves_unicode_in_json(fake_repo):
    """中文 query / answer / chunk_ids 不能被转 \\uXXXX，DB 里要保留原文。"""
    audit_log.log_qa(
        kb_id="agv_demo",
        session_id="sess_xx",
        query="电池怎么换？",
        answer="按蓝色按钮",
        chunk_ids=["电池SOP_step_1"],
    )
    kwargs = fake_repo.append.call_args.kwargs
    # ensure_ascii=False 保留中文原文
    assert "电池SOP_step_1" in kwargs["chunk_ids_json"]


# ---------------------------------------------------------------------------
# AuditRepository 接口 — 用 SQLite in-memory provider 跑端到端
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_provider(tmp_path, monkeypatch):
    """临时 SQLite 文件 + 自定义 provider，验证真实 SQL（含 ORDER BY、过滤）。"""
    import contextlib
    import sqlite3

    db_path = tmp_path / "audit_test.sqlite"
    # 建表
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE audit_logs (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          ts          TEXT NOT NULL,
          tenant_id   TEXT NOT NULL DEFAULT 'default',
          session_id  TEXT NOT NULL DEFAULT '',
          kb_id       TEXT NOT NULL DEFAULT '',
          event_type  TEXT NOT NULL,
          query       TEXT NOT NULL DEFAULT '',
          answer      TEXT NOT NULL DEFAULT '',
          chunk_ids   TEXT NOT NULL DEFAULT '[]',
          meta        TEXT NOT NULL DEFAULT '{}',
          user_id     TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.commit()
    conn.close()

    class _TestProvider:
        placeholder = "?"
        backend_name = "sqlite"

        @contextlib.contextmanager
        def connect(self):
            c = sqlite3.connect(str(db_path))
            c.row_factory = sqlite3.Row
            try:
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise
            finally:
                c.close()

    return _TestProvider()


def test_repository_append_then_list(sqlite_provider):
    from custom_app.repositories.audit_repository import AuditRepository
    repo = AuditRepository(provider=sqlite_provider)
    repo.append(
        ts="2026-06-01T10:00:00", event_type="qa",
        kb_id="kb_a", session_id="s1", query="q1", answer="a1",
        chunk_ids_json='["c1","c2"]', meta_json='{"x":1}',
    )
    repo.append(
        ts="2026-06-01T10:01:00", event_type="qa",
        kb_id="kb_a", session_id="s1", query="q2", answer="a2",
    )
    repo.append(
        ts="2026-06-01T10:02:00", event_type="qa",
        kb_id="kb_b", session_id="s2", query="q3", answer="a3",
    )

    rows = repo.list_recent()
    # ORDER BY id DESC
    assert len(rows) == 3
    assert rows[0]["query"] == "q3"
    assert rows[2]["query"] == "q1"


def test_repository_filter_by_kb_id(sqlite_provider):
    from custom_app.repositories.audit_repository import AuditRepository
    repo = AuditRepository(provider=sqlite_provider)
    for q, kb in [("qa1", "kb_a"), ("qb1", "kb_b"), ("qa2", "kb_a")]:
        repo.append(
            ts="2026-06-01T10:00:00", event_type="qa",
            kb_id=kb, query=q, answer="a",
        )

    rows = repo.list_recent(kb_id="kb_a")
    assert [r["query"] for r in rows] == ["qa2", "qa1"]


def test_repository_filter_by_session_id(sqlite_provider):
    from custom_app.repositories.audit_repository import AuditRepository
    repo = AuditRepository(provider=sqlite_provider)
    repo.append(ts="t", event_type="qa", session_id="s1", query="x")
    repo.append(ts="t", event_type="qa", session_id="s2", query="y")
    repo.append(ts="t", event_type="qa", session_id="s1", query="z")

    rows = repo.list_recent(session_id="s1")
    assert {r["query"] for r in rows} == {"x", "z"}


def test_repository_count(sqlite_provider):
    from custom_app.repositories.audit_repository import AuditRepository
    repo = AuditRepository(provider=sqlite_provider)
    for i in range(7):
        repo.append(ts="t", event_type="qa", kb_id="k", query=f"q{i}")

    assert repo.count() == 7
    assert repo.count(kb_id="k") == 7
    assert repo.count(kb_id="other") == 0


def test_repository_no_update_delete_methods(sqlite_provider):
    """合规约束：AuditRepository 不应暴露 update / delete 接口。"""
    from custom_app.repositories.audit_repository import AuditRepository
    repo = AuditRepository(provider=sqlite_provider)
    # 反向验证：以下属性不应存在
    assert not hasattr(repo, "update")
    assert not hasattr(repo, "delete")
    assert not hasattr(repo, "delete_by_id")
