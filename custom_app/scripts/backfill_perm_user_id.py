"""P-Perm Commit 6：把存量未归属（user_id='' / NULL）的 session/audit 数据归到 admin。

背景：
    Commit 1-5 上线后，新会话/审计在创建时已写 user_id；但 Commit 1 之前生成的
    数据 user_id 仍为空。如果直接收紧权限：
      - /api/sessions 列表会把这些旧会话隐藏（owner 过滤）
      - /api/sessions/<id> GET 会 403
    本脚本一次性把它们归到 admin 用户（即默认种子账号），让管理员仍可在 UI
    中看到与处理。

适配后端：
    - SQLite：本地默认；按 ULTRARAG_DB_BACKEND 不显式指定时走 SQLite
    - Postgres awprag：ULTRARAG_DB_BACKEND=postgres 时

用法：
    DRY-RUN：    python -m custom_app.scripts.backfill_perm_user_id
    实际写入：   python -m custom_app.scripts.backfill_perm_user_id --apply
    指定接管人： python -m custom_app.scripts.backfill_perm_user_id --apply --owner admin

输出：
    迁移前后影响行数 + 每张表的统计；非 admin 用户行不被触碰。
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from custom_app.repositories.base import (
    adapt_sql,
    get_default_provider,
)
from custom_app.repositories.user_repository import UserRepository

logger = logging.getLogger(__name__)


def _resolve_owner_id(username: str) -> Optional[str]:
    """根据 username 查 user_id；不存在返回 None。"""
    repo = UserRepository()
    row = repo.find_by_username(username)
    if not row:
        return None
    return str(row["user_id"])


def _count_unassigned(provider, table: str) -> int:
    """统计表中 user_id 为空 / NULL 的行数。"""
    sql = (
        f"SELECT COUNT(*) AS n FROM {table} "
        f"WHERE user_id IS NULL OR user_id = ''"
    )
    with provider.connect() as conn:
        cur = conn.execute(adapt_sql(sql, provider))
        row = cur.fetchone()
        if row is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, IndexError):
            return int(row["n"])


def _assign(provider, table: str, owner_id: str) -> int:
    """把表中 user_id 为空 / NULL 的行更新到 owner_id；返回受影响行数。"""
    sql = (
        f"UPDATE {table} SET user_id = ? "
        f"WHERE user_id IS NULL OR user_id = ''"
    )
    with provider.connect() as conn:
        cur = conn.execute(adapt_sql(sql, provider), (owner_id,))
        try:
            return int(getattr(cur, "rowcount", 0) or 0)
        except Exception:  # noqa: BLE001
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="P-Perm Commit 6 backfill: assign legacy sessions/audit rows to an owner user.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="实际写入；默认仅 dry-run 报告会改多少行。",
    )
    parser.add_argument(
        "--owner", default="admin",
        help="接管人 username（默认 admin）。该用户必须已存在。",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    provider = get_default_provider()
    logger.info("backend: %s", getattr(provider, "backend_name", "unknown"))

    owner_id = _resolve_owner_id(args.owner)
    if not owner_id:
        logger.error(
            "owner username '%s' not found in users table; create one first via /api/admin/users",
            args.owner,
        )
        return 1
    logger.info("owner '%s' resolved to user_id=%s", args.owner, owner_id)

    tables = ("kb_sessions", "audit_logs")
    before = {t: _count_unassigned(provider, t) for t in tables}
    logger.info("rows to backfill: %s", before)

    if not args.apply:
        logger.info("DRY-RUN: pass --apply to actually update.")
        return 0

    after = {}
    for t in tables:
        affected = _assign(provider, t, owner_id)
        after[t] = affected
        logger.info("table=%s updated_rows=%d", t, affected)

    # 复查：剩余 0
    remaining = {t: _count_unassigned(provider, t) for t in tables}
    logger.info("post-apply remaining unassigned: %s", remaining)
    if any(v > 0 for v in remaining.values()):
        logger.warning(
            "some rows still unassigned (may be due to concurrent writes); re-run if needed",
        )
        return 2
    logger.info("backfill complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
