"""Phase 6.1 migration runner: 应用 migrations/postgres/001_phase6_1_doc_status.sql。

只对 Postgres 后端有用；SQLite 由 init_db() 自动 ALTER。
跑完后会验证 kb_documents 表里两列已经存在。
"""

from __future__ import annotations

import os
import pathlib
import sys

from dotenv import load_dotenv
import psycopg


def main() -> int:
    load_dotenv()
    uri = os.environ.get("ULTRARAG_POSTGRES_URI", "").strip()
    if not uri:
        print("ULTRARAG_POSTGRES_URI not set in env/.env", file=sys.stderr)
        return 1

    sql_path = pathlib.Path("migrations/postgres/001_phase6_1_doc_status.sql")
    if not sql_path.exists():
        print(f"migration not found: {sql_path}", file=sys.stderr)
        return 1

    raw = sql_path.read_text(encoding="utf-8")
    # 剥掉 `--` 单行注释；psycopg 单条 execute 不接受注释独占的"语句"。
    lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("--")]
    sql = "\n".join(lines)

    with psycopg.connect(uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            for stmt in sql.split(";"):
                s = stmt.strip()
                if not s:
                    continue
                cur.execute(s)
                print(f"OK: {s.splitlines()[0][:80]}")

            cur.execute(
                """
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_name = 'kb_documents'
                  AND column_name IN ('processed_at', 'chunk_count')
                ORDER BY column_name
                """
            )
            rows = cur.fetchall()
            if len(rows) != 2:
                print("VERIFY FAILED: missing columns", rows, file=sys.stderr)
                return 2
            for r in rows:
                print(f"verify: {r}")

    print("Phase 6.1 migration applied successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
