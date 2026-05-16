"""Phase 7 migration runner: 应用 migrations/postgres/003_phase7_chat_models.sql。

SQLite 由 init_db() 自动建表，无需手工跑。
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

    sql_path = pathlib.Path("migrations/postgres/003_phase7_chat_models.sql")
    if not sql_path.exists():
        print(f"migration not found: {sql_path}", file=sys.stderr)
        return 1

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
                print(f"OK: {s.splitlines()[0][:80]}")

            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_name = 'chat_models'
                """
            )
            row = cur.fetchone()
            if not row or row[0] == 0:
                print("VERIFY FAILED: chat_models table missing", file=sys.stderr)
                return 2
            print(f"verify: chat_models table exists")

    print("Phase 7 migration applied successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
