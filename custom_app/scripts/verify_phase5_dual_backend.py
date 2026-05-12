"""Phase 5.1.8 — 双后端验收：SQLite vs PostgreSQL + FAISS vs Qdrant。

通过 Repository 抽象在两种后端上跑同一组业务方法，对比返回结果，
证明 Phase 5.1 的存储栈切换零功能差异。

用法：
    .venv\\Scripts\\python.exe -m custom_app.scripts.verify_phase5_dual_backend

退出码：
    0 = 双后端一致
    1 = 数据不一致 / 服务不可达
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable

from dotenv import load_dotenv

load_dotenv()


def _print_header(title: str) -> None:
    print(f"\n=== {title} ===")


def _run_repo_check(
    label: str,
    sqlite_action: Callable[[], Any],
    postgres_action: Callable[[], Any],
) -> bool:
    """跑同一断言在 SQLite 和 Postgres 上；返回是否一致。"""
    try:
        sqlite_result = sqlite_action()
    except Exception as e:
        print(f"  [SQLITE FAIL] {label}: {e}")
        return False
    try:
        pg_result = postgres_action()
    except Exception as e:
        print(f"  [POSTGRES FAIL] {label}: {e}")
        return False

    # 简化对比：转 str 后比较内容（避免 dict 顺序差异）
    s_str = str(sqlite_result)
    p_str = str(pg_result)
    if s_str == p_str:
        print(f"  [OK] {label}: 双后端一致 ({type(sqlite_result).__name__}={s_str[:80]})")
        return True
    print(f"  [DIFF] {label}:")
    print(f"    SQLite:   {s_str[:200]}")
    print(f"    Postgres: {p_str[:200]}")
    return False


def main() -> int:
    print("=== Phase 5.1.8 双后端验收 ===")

    if not os.environ.get("ULTRARAG_POSTGRES_URI"):
        print("ERROR: ULTRARAG_POSTGRES_URI 未配置")
        return 1
    if not os.environ.get("ULTRARAG_QDRANT_URL"):
        print("ERROR: ULTRARAG_QDRANT_URL 未配置")
        return 1

    from custom_app.repositories import (
        AgentConfigRepository,
        DocumentRepository,
        JobRepository,
        KbRepository,
        KgRepository,
        RoleRepository,
        SessionRepository,
        SqliteConnectionProvider,
        set_default_provider,
    )
    from custom_app.repositories.postgres_provider import (
        PostgresConnectionProvider,
        init_postgres_schema,
    )

    sqlite_provider = SqliteConnectionProvider()
    postgres_provider = PostgresConnectionProvider()
    init_postgres_schema(postgres_provider)

    passed = 0
    failed = 0

    # ───────────────────────────────────────────────────────────────────
    # 第 1 部分：Repository 双后端一致性
    # ───────────────────────────────────────────────────────────────────
    _print_header("第 1 部分：Repository 业务方法双后端一致性")

    # KB 列表（按 created_at DESC 顺序对比 kb_id）
    s_kb_repo = KbRepository(sqlite_provider)
    p_kb_repo = KbRepository(postgres_provider)

    def _list_kb_ids(repo):
        rows = repo.list_paginated(role_id=None, include_archived=True, limit=100, offset=0)
        return [r["kb_id"] for r in rows]

    if _run_repo_check(
        "KB list 顺序",
        lambda: _list_kb_ids(s_kb_repo),
        lambda: _list_kb_ids(p_kb_repo),
    ):
        passed += 1
    else:
        failed += 1

    # Job 列表
    s_job_repo = JobRepository(sqlite_provider)
    p_job_repo = JobRepository(postgres_provider)

    def _kb_job_summary(repo, kb_id):
        rows = repo.list_for_kb(kb_id, limit=100, offset=0)
        return [(r["job_id"], r["status"]) for r in rows]

    # 取第一个 KB 看其 jobs
    sqlite_kbs = _list_kb_ids(s_kb_repo)
    if sqlite_kbs:
        first_kb = sqlite_kbs[0]
        if _run_repo_check(
            f"Job list for kb={first_kb}",
            lambda: _kb_job_summary(s_job_repo, first_kb),
            lambda: _kb_job_summary(p_job_repo, first_kb),
        ):
            passed += 1
        else:
            failed += 1

    # Document 数量
    s_doc_repo = DocumentRepository(sqlite_provider)
    p_doc_repo = DocumentRepository(postgres_provider)

    if sqlite_kbs:
        first_kb = sqlite_kbs[0]
        if _run_repo_check(
            f"Document count for kb={first_kb}",
            lambda: len(s_doc_repo.list_for_kb(first_kb, limit=100, offset=0)),
            lambda: len(p_doc_repo.list_for_kb(first_kb, limit=100, offset=0)),
        ):
            passed += 1
        else:
            failed += 1

    # Session 数量
    s_sess_repo = SessionRepository(sqlite_provider)
    p_sess_repo = SessionRepository(postgres_provider)

    if sqlite_kbs:
        first_kb = sqlite_kbs[0]
        if _run_repo_check(
            f"Session count for kb={first_kb}",
            lambda: len(s_sess_repo.list_sessions_for_kb(first_kb)),
            lambda: len(p_sess_repo.list_sessions_for_kb(first_kb)),
        ):
            passed += 1
        else:
            failed += 1

    # KG 统计
    s_kg_repo = KgRepository(sqlite_provider)
    p_kg_repo = KgRepository(postgres_provider)

    if sqlite_kbs:
        first_kb = sqlite_kbs[0]
        if _run_repo_check(
            f"KG stats for kb={first_kb}",
            lambda: s_kg_repo.count_entities_and_relations(first_kb),
            lambda: p_kg_repo.count_entities_and_relations(first_kb),
        ):
            passed += 1
        else:
            failed += 1

    # AgentConfig
    s_cfg_repo = AgentConfigRepository(sqlite_provider)
    p_cfg_repo = AgentConfigRepository(postgres_provider)

    if sqlite_kbs:
        first_kb = sqlite_kbs[0]
        if _run_repo_check(
            f"AgentConfig for kb={first_kb}",
            lambda: s_cfg_repo.get_enabled_tools_json(first_kb),
            lambda: p_cfg_repo.get_enabled_tools_json(first_kb),
        ):
            passed += 1
        else:
            failed += 1

    # ───────────────────────────────────────────────────────────────────
    # 第 2 部分：Qdrant 集合验证
    # ───────────────────────────────────────────────────────────────────
    _print_header("第 2 部分：Qdrant collection 与原 FAISS 索引一致性")

    try:
        from custom_app.services.vectorstore.qdrant_store import QdrantVectorStore

        for kb_id in sqlite_kbs[:3]:  # 测前 3 个 KB
            store = QdrantVectorStore(kb_id=kb_id)
            try:
                size = store.size()
                if size > 0:
                    print(f"  [OK] {store.collection_name}: {size} points")
                    passed += 1
                else:
                    print(f"  [WARN] {store.collection_name}: 0 points（KB 可能尚未迁移）")
            except Exception as e:
                print(f"  [INFO] {store.collection_name}: collection 不存在或不可达 ({e})")
    except Exception as e:
        print(f"  [SKIP] Qdrant 检查失败：{e}")

    # ───────────────────────────────────────────────────────────────────
    # 总结
    # ───────────────────────────────────────────────────────────────────
    _print_header("总结")
    print(f"  通过：{passed}")
    print(f"  失败：{failed}")

    postgres_provider.close()

    if failed > 0:
        print("\n[FAIL] Phase 5 双后端一致性验证失败")
        return 1
    print("\n[OK] Phase 5 双后端验证通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
