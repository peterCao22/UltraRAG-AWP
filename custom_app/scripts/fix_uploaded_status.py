"""一次性修复：把老的 status='uploaded' 行改为 'pending'，与 Phase 6.1 枚举对齐。

幂等可重复运行。
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
import psycopg


def main() -> int:
    load_dotenv()
    uri = os.environ.get("ULTRARAG_POSTGRES_URI", "").strip()
    if not uri:
        print("ULTRARAG_POSTGRES_URI not set", file=sys.stderr)
        return 1
    with psycopg.connect(uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE kb_documents SET status='pending' WHERE status='uploaded'"
            )
            print(f"updated {cur.rowcount} rows: uploaded -> pending")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
