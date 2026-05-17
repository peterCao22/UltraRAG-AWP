"""Phase 7.2.A migration runner: 应用 004_phase7_2_a_agent_configs.sql + 种子数据。

SQLite 由 init_db() 自动建表 + 种子；本脚本仅服务 Postgres awprag 部署。

行为：
    1. 跑 migrations/postgres/004_phase7_2_a_agent_configs.sql（CREATE 幂等）
    2. 插入 builtin-quick / builtin-agent 两行；已存在则 ON CONFLICT 跳过
    3. 校验：agent_configs 表存在 + 至少有 2 行 builtin
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys
from typing import Any

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


def _builtin_rows(now: str) -> list[dict[str, Any]]:
    """复用 db._builtin_agent_seed_rows，避免两份种子定义漂移。"""
    from custom_app.db import _builtin_agent_seed_rows

    rows = _builtin_agent_seed_rows(now)
    # Postgres 用 BOOLEAN，SQLite seed 用 INTEGER 0/1；这里转 bool
    for r in rows:
        r["is_builtin"] = bool(r["is_builtin"])
        r["enabled"] = bool(r["enabled"])
    return rows


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    uri = os.environ.get("ULTRARAG_POSTGRES_URI", "").strip()
    if not uri:
        print("ULTRARAG_POSTGRES_URI not set in env/.env", file=sys.stderr)
        return 1

    sql_path = pathlib.Path("migrations/postgres/004_phase7_2_a_agent_configs.sql")
    if not sql_path.exists():
        print(f"migration not found: {sql_path}", file=sys.stderr)
        return 1

    import psycopg
    from custom_app.db import now_iso

    raw = sql_path.read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("--")]
    sql = "\n".join(lines)

    with psycopg.connect(uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            for stmt in sql.split(";"):
                s = stmt.strip()
                if not s:
                    continue
                cur.execute(s)
                logger.info("OK: %s", s.splitlines()[0][:80])

            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_name = 'agent_configs'
                """
            )
            row = cur.fetchone()
            if not row or row[0] == 0:
                print("VERIFY FAILED: agent_configs table missing", file=sys.stderr)
                return 2
            logger.info("verify: agent_configs table exists")

            now = now_iso()
            for r in _builtin_rows(now):
                cur.execute(
                    """
                    INSERT INTO agent_configs
                      (agent_id, tenant_id, name, description, avatar, agent_mode,
                       is_builtin, system_prompt, agent_system_prompt, model_id,
                       temperature, max_tokens, enabled, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s)
                    ON CONFLICT (agent_id) DO NOTHING
                    """,
                    (
                        r["agent_id"], r["tenant_id"], r["name"], r["description"],
                        r["avatar"], r["agent_mode"], r["is_builtin"],
                        r["system_prompt"], r["agent_system_prompt"], r["model_id"],
                        r["temperature"], r["max_tokens"], r["enabled"],
                        r["created_at"], r["updated_at"],
                    ),
                )
                logger.info("seed: %s", r["agent_id"])

            cur.execute(
                "SELECT COUNT(*) FROM agent_configs WHERE is_builtin = TRUE"
            )
            builtin_row = cur.fetchone()
            builtin_count = int(builtin_row[0]) if builtin_row else 0
            if builtin_count < 2:
                print(
                    f"VERIFY FAILED: expected >= 2 builtin agents, got {builtin_count}",
                    file=sys.stderr,
                )
                return 3
            logger.info("verify: builtin agent count = %d", builtin_count)

    print("Phase 7.2.A migration applied successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
