"""Phase 6.2 migration runner: 应用 migrations/postgres/002_phase6_2_kg_doc_id.sql。

只对 Postgres 后端有用；SQLite 由 init_db() 自动 ALTER。
跑完后会验证 kg_relations 表里 doc_id 列已经存在。
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

    sql_path = pathlib.Path("migrations/postgres/002_phase6_2_kg_doc_id.sql")
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
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = 'kg_relations' AND column_name = 'doc_id'
                """
            )
            rows = cur.fetchall()
            if not rows:
                print("VERIFY FAILED: kg_relations.doc_id missing", file=sys.stderr)
                return 2
            for r in rows:
                print(f"verify: {r}")

    print("Phase 6.2 migration applied successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
