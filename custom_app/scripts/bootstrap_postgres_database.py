"""Phase 5.1 — 在 PostgreSQL 服务器上创建项目专用数据库（首次部署用）。

业务约定：
    - 项目使用专用数据库 `awprag`（全小写，避免 PG 大小写引号问题）
    - 不使用默认 `postgres` 库，保持系统默认库干净

用法：
    .venv\\Scripts\\python.exe -m custom_app.scripts.bootstrap_postgres_database

    # 自定义数据库名：
    .venv\\Scripts\\python.exe -m custom_app.scripts.bootstrap_postgres_database --db mycustom

读取环境变量 ULTRARAG_POSTGRES_URI 中 host/credentials，
但**始终连接默认 postgres 库**执行 CREATE DATABASE（PG 限制：
不能在目标库内创建自身）。

退出码：
    0  创建成功 / 已存在
    1  连接失败 / 权限不足
    2  参数错误
"""

from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv

load_dotenv()


def _admin_uri_from_env() -> str:
    """从 ULTRARAG_POSTGRES_URI 拿 host/credentials，但路径强制改为 /postgres。

    管理员操作必须连默认 postgres 库（不能在目标库内 CREATE DATABASE 自身）。
    """
    uri = os.environ.get("ULTRARAG_POSTGRES_URI", "").strip()
    if not uri:
        print("ERROR: ULTRARAG_POSTGRES_URI not set in environment", file=sys.stderr)
        sys.exit(1)
    parsed = urlparse(uri)
    admin_parsed = parsed._replace(path="/postgres")
    return urlunparse(admin_parsed)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="创建 PostgreSQL 项目专用数据库（默认 awprag）"
    )
    parser.add_argument(
        "--db",
        default="awprag",
        help="目标数据库名（默认 awprag）",
    )
    parser.add_argument(
        "--encoding",
        default="UTF8",
        help="数据库编码（默认 UTF8）",
    )
    args = parser.parse_args()

    db_name = args.db.strip()
    if not db_name:
        print("ERROR: --db 不能为空", file=sys.stderr)
        return 2

    # PG 数据库名需要小写（避免后续每次访问要双引号）
    if db_name != db_name.lower():
        print(
            f"WARN: 推荐使用全小写数据库名，{db_name!r} 含大写字母。"
            "若坚持使用，所有 psql 命令必须用双引号包裹。",
            file=sys.stderr,
        )

    try:
        import psycopg
    except ImportError as e:
        print(f"ERROR: psycopg not installed: {e}", file=sys.stderr)
        print("  安装：uv sync --extras storage", file=sys.stderr)
        return 1

    admin_uri = _admin_uri_from_env()
    print(f"=== Bootstrap PostgreSQL database ===")
    print(f"  Target db: {db_name}")
    print(f"  Encoding:  {args.encoding}")
    # 不打印完整 URI（含密码），只显示 host:port
    parsed = urlparse(admin_uri)
    print(f"  Server:    {parsed.hostname}:{parsed.port}")

    try:
        # autocommit=True 才能 CREATE DATABASE
        with psycopg.connect(admin_uri, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)
                )
                if cur.fetchone() is not None:
                    print(f"  [OK] Database '{db_name}' already exists (no-op)")
                else:
                    # 不能用占位符；标识符必须直接拼接（已校验过 db_name 安全）
                    if not all(c.isalnum() or c == "_" for c in db_name):
                        print(
                            f"ERROR: 数据库名包含非法字符 {db_name!r}; "
                            "仅允许 [a-zA-Z0-9_]",
                            file=sys.stderr,
                        )
                        return 2
                    cur.execute(
                        f"CREATE DATABASE {db_name} "
                        f"ENCODING '{args.encoding}' TEMPLATE template0"
                    )
                    print(f"  [OK] Created database '{db_name}'")
                # 列出最终状态
                cur.execute(
                    "SELECT datname FROM pg_database "
                    "WHERE datistemplate=false ORDER BY datname"
                )
                all_dbs = [r[0] for r in cur.fetchall()]
                print(f"  All databases: {all_dbs}")
    except psycopg.OperationalError as e:
        print(f"ERROR: Connection failed: {e}", file=sys.stderr)
        return 1
    except psycopg.Error as e:
        print(f"ERROR: PostgreSQL error: {e}", file=sys.stderr)
        return 1

    print("\n下一步：")
    print(f"  1. 把 ULTRARAG_POSTGRES_URI 末尾改为 /{db_name}")
    print(f"  2. 跑数据迁移：python -m custom_app.scripts.migrate_sqlite_to_postgres")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
