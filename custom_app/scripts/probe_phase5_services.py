"""Phase 5.1.0 — Qdrant + PostgreSQL 局域网服务连通性探测。

在动代码前先确认两个 Docker 服务可达，避免后续集成时排障牵扯到网络。

用法：
    .venv\\Scripts\\python.exe -m custom_app.scripts.probe_phase5_services

输出：
    - Qdrant: 版本号 + collections 列表
    - Postgres: 版本号 + 当前 DB 列表

退出码 0 = 全部可达，非 0 = 至少一项失败。
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------


def probe_qdrant() -> Optional[str]:
    """探测 Qdrant 服务；返回错误消息（None = 成功）。"""
    import requests

    url = os.environ.get("ULTRARAG_QDRANT_URL", "")
    if not url:
        return "ULTRARAG_QDRANT_URL not set"
    api_key = os.environ.get("ULTRARAG_QDRANT_API_KEY", "")
    timeout = int(os.environ.get("ULTRARAG_QDRANT_TIMEOUT_SEC", "10"))

    headers = {"api-key": api_key} if api_key else {}

    try:
        # GET / 返回版本信息
        r = requests.get(url.rstrip("/"), headers=headers, timeout=timeout)
        r.raise_for_status()
        info = r.json()
        title = info.get("title", "qdrant")
        version = info.get("version", "?")
        print(f"  [OK] {title} version={version} at {url}")

        # 列出 collections
        r2 = requests.get(
            f"{url.rstrip('/')}/collections", headers=headers, timeout=timeout
        )
        r2.raise_for_status()
        cols = r2.json().get("result", {}).get("collections", [])
        if cols:
            print(f"  existing collections: {[c['name'] for c in cols]}")
        else:
            print("  existing collections: (none)")
        return None
    except requests.RequestException as e:
        return f"qdrant request failed: {e}"
    except (KeyError, ValueError) as e:
        return f"qdrant unexpected response: {e}"


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


def probe_postgres() -> Optional[str]:
    """探测 PostgreSQL 服务；返回错误消息（None = 成功）。"""
    uri = os.environ.get("ULTRARAG_POSTGRES_URI", "")
    if not uri:
        return "ULTRARAG_POSTGRES_URI not set"

    try:
        import psycopg  # type: ignore
    except ImportError:
        return "psycopg not installed (pip install 'psycopg[binary]')"

    try:
        with psycopg.connect(uri, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                version = cur.fetchone()[0]
                print(f"  [OK] {version}")
                cur.execute("SELECT current_database()")
                db = cur.fetchone()[0]
                print(f"  current database: {db}")
                cur.execute(
                    "SELECT schemaname, tablename FROM pg_tables "
                    "WHERE schemaname='public' ORDER BY tablename"
                )
                tables = cur.fetchall()
                if tables:
                    print(f"  existing public tables: {[t[1] for t in tables]}")
                else:
                    print("  existing public tables: (none)")
        return None
    except psycopg.Error as e:
        return f"postgres connection failed: {e}"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    print("=== Phase 5.1.0 服务连通性探测 ===\n")

    print("[Qdrant]")
    qdrant_url = os.environ.get("ULTRARAG_QDRANT_URL", "(unset)")
    print(f"  URL: {qdrant_url}")
    qdrant_err = probe_qdrant()
    print()

    print("[PostgreSQL]")
    # 不打印完整 URI（含密码），只显示 host:port/db
    pg_uri = os.environ.get("ULTRARAG_POSTGRES_URI", "(unset)")
    # 简单 mask
    if "@" in pg_uri:
        masked = pg_uri[: pg_uri.find("://") + 3] + "***@" + pg_uri.split("@", 1)[1]
    else:
        masked = pg_uri
    print(f"  URI: {masked}")
    pg_err = probe_postgres()
    print()

    print("=== 结果 ===")
    failed = []
    if qdrant_err:
        print(f"[FAIL] Qdrant: {qdrant_err}")
        failed.append("qdrant")
    else:
        print("[OK]   Qdrant")
    if pg_err:
        print(f"[FAIL] Postgres: {pg_err}")
        failed.append("postgres")
    else:
        print("[OK]   Postgres")

    if failed:
        print(f"\n失败服务: {failed}")
        print("请检查 .env 中 ULTRARAG_QDRANT_URL / ULTRARAG_POSTGRES_URI 配置")
        return 1
    print("\n两个服务都可达，可以开始 Phase 5.1 后续任务。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
