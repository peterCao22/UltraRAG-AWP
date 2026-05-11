"""PostgreSQL ConnectionProvider —— 用 psycopg3 + 连接池。

依赖：psycopg[binary,pool]，已加入 pyproject.toml [storage] extras。
连接 URI 从 ULTRARAG_POSTGRES_URI 环境变量读取。

设计要点：
    - 用 dict_row factory，让 fetchall() 直接返回 dict（兼容现有 row_to_dict 风格）
    - 连接池单例（ConnectionPool），避免每次请求新建连接
    - placeholder = "%s"（psycopg 风格），adapt_sql 把 SQL 中 "?" 替换
    - schema 初始化由 init_postgres_schema() 显式调用（与 SQLite init_db 类似）
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)


class PostgresConnectionProvider:
    """PostgreSQL 后端 —— 通过 psycopg_pool 复用连接。"""

    placeholder = "%s"
    backend_name = "postgres"

    def __init__(self, uri: Optional[str] = None) -> None:
        self._uri = uri or os.environ.get("ULTRARAG_POSTGRES_URI", "").strip()
        if not self._uri:
            raise ValueError("ULTRARAG_POSTGRES_URI not set in environment")
        self._pool = self._build_pool()

    def _build_pool(self):
        from psycopg_pool import ConnectionPool  # type: ignore

        # min_size=1：保证启动后有热连接；max_size=10：单 Flask 进程够用
        # open=False + 显式 pool.open(wait=True) 避免 deprecation 警告
        pool = ConnectionPool(
            self._uri,
            min_size=1,
            max_size=10,
            timeout=30,
            kwargs={"autocommit": False},
            open=False,
        )
        pool.open(wait=True, timeout=30)
        return pool

    @contextlib.contextmanager
    def connect(self) -> Iterator[Any]:
        from psycopg.rows import dict_row  # type: ignore

        with self._pool.connection() as conn:
            # 设置 row_factory 让 fetchall 返回 list[dict]
            conn.row_factory = dict_row
            # 用 PgConnectionAdapter 包装让 conn.execute() 行为接近 sqlite3.Connection
            adapter = _PgConnectionAdapter(conn)
            try:
                yield adapter
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def close(self) -> None:
        """显式关闭连接池（一般在测试/重置时调用）。"""
        if self._pool:
            self._pool.close()


class _PgConnectionAdapter:
    """让 psycopg connection 的 execute() API 接近 sqlite3.Connection。

    sqlite3 接口：
        cur = conn.execute(sql, params)      # 返回 cursor，可 fetchone/fetchall
        conn.commit() / conn.rollback() / conn.close()

    psycopg3 接口：
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cur.fetchone() / cur.fetchall()

    通过 adapter 让 Repository 写法两种后端通用。
    """

    def __init__(self, pg_conn) -> None:
        self._conn = pg_conn

    def execute(self, sql: str, params: tuple = ()):
        """执行 SQL 并返回 cursor（兼容 sqlite3 接口）。

        注意：psycopg cursor 不能脱离 with 块使用；这里直接打开 cursor 不关闭，
        让 Repository 调用方在同一 with 块内 fetch（实际就是这样用的）。
        在 connect() 的 with 退出时连接归还，cursor 自动关闭。
        """
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return cur


# ---------------------------------------------------------------------------
# schema 初始化（Postgres 翻译版）
# ---------------------------------------------------------------------------


_POSTGRES_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS knowledge_bases (
  id              SERIAL PRIMARY KEY,
  kb_id           TEXT NOT NULL UNIQUE,
  name            TEXT NOT NULL,
  description     TEXT DEFAULT '',
  tenant_id       TEXT NOT NULL DEFAULT 'default',
  status          TEXT NOT NULL DEFAULT 'active',
  type            TEXT NOT NULL DEFAULT 'sop_docx',
  data_path       TEXT NOT NULL,
  index_path      TEXT DEFAULT '',
  embedding_path  TEXT DEFAULT '',
  last_indexed_at TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kb_jobs (
  id            SERIAL PRIMARY KEY,
  job_id        TEXT NOT NULL UNIQUE,
  tenant_id     TEXT NOT NULL DEFAULT 'default',
  kb_id         TEXT NOT NULL,
  job_type      TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending',
  retry_count   INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT DEFAULT '',
  payload_json  TEXT DEFAULT '{}',
  result_json   TEXT DEFAULT '{}',
  started_at    TEXT,
  finished_at   TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kb_documents (
  id            SERIAL PRIMARY KEY,
  kb_id         TEXT NOT NULL,
  tenant_id     TEXT NOT NULL DEFAULT 'default',
  doc_id        TEXT NOT NULL,
  file_name     TEXT NOT NULL,
  file_type     TEXT NOT NULL,
  file_path     TEXT NOT NULL,
  channel       TEXT NOT NULL DEFAULT 'api',
  status        TEXT NOT NULL DEFAULT 'pending',
  error_message TEXT DEFAULT '',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE (kb_id, doc_id)
);

CREATE INDEX IF NOT EXISTS idx_kb_tenant_status
  ON knowledge_bases (tenant_id, status);

CREATE INDEX IF NOT EXISTS idx_job_kb_status
  ON kb_jobs (kb_id, status);

CREATE INDEX IF NOT EXISTS idx_doc_kb_status
  ON kb_documents (kb_id, status);

CREATE TABLE IF NOT EXISTS roles (
  id          SERIAL PRIMARY KEY,
  role_id     TEXT NOT NULL UNIQUE,
  name        TEXT NOT NULL UNIQUE,
  description TEXT DEFAULT '',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS role_kb_permissions (
  id           SERIAL PRIMARY KEY,
  role_id      TEXT NOT NULL,
  kb_id        TEXT NOT NULL,
  access_level TEXT NOT NULL DEFAULT 'read',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  UNIQUE (role_id, kb_id)
);

CREATE INDEX IF NOT EXISTS idx_role_kb_perm
  ON role_kb_permissions (role_id, kb_id);

CREATE TABLE IF NOT EXISTS kb_sessions (
  session_id TEXT NOT NULL PRIMARY KEY,
  kb_id      TEXT NOT NULL,
  title      TEXT NOT NULL DEFAULT '',
  agent_mode TEXT NOT NULL DEFAULT 'quick',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kb_sessions_kb_updated
  ON kb_sessions (kb_id, updated_at);

CREATE TABLE IF NOT EXISTS kb_session_messages (
  id             SERIAL PRIMARY KEY,
  session_id     TEXT NOT NULL,
  role           TEXT NOT NULL,
  content        TEXT NOT NULL,
  reasoning_json TEXT NOT NULL DEFAULT '{}',
  created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kb_sess_msg_sid
  ON kb_session_messages (session_id);

CREATE TABLE IF NOT EXISTS kb_agent_configs (
  kb_id              TEXT NOT NULL PRIMARY KEY,
  enabled_tools_json TEXT NOT NULL DEFAULT '[]',
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kg_entities (
  id          SERIAL PRIMARY KEY,
  kb_id       TEXT NOT NULL,
  entity_name TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  description TEXT DEFAULT '',
  chunk_ids   TEXT NOT NULL DEFAULT '[]',
  created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kg_entity_kb ON kg_entities(kb_id);
CREATE INDEX IF NOT EXISTS idx_kg_entity_name ON kg_entities(entity_name);

CREATE TABLE IF NOT EXISTS kg_relations (
  id            SERIAL PRIMARY KEY,
  kb_id         TEXT NOT NULL,
  source_id     INTEGER NOT NULL,
  target_id     INTEGER NOT NULL,
  relation_type TEXT NOT NULL,
  description   TEXT DEFAULT '',
  strength      INTEGER DEFAULT 5,
  created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kg_rel_kb ON kg_relations(kb_id);
CREATE INDEX IF NOT EXISTS idx_kg_rel_source ON kg_relations(source_id);
CREATE INDEX IF NOT EXISTS idx_kg_rel_target ON kg_relations(target_id);
"""


def init_postgres_schema(provider: Optional[PostgresConnectionProvider] = None) -> None:
    """在 Postgres 上建 schema（CREATE TABLE IF NOT EXISTS，幂等）。"""
    if provider is None:
        provider = PostgresConnectionProvider()
    with provider.connect() as adapter:
        # psycopg 不支持单 execute 跑多语句；按分号切分逐条执行
        for stmt in _POSTGRES_SCHEMA_DDL.split(";"):
            stmt = stmt.strip()
            if stmt:
                adapter.execute(stmt + ";")
    logger.info("postgres schema initialized")


# 让 _PgConnectionAdapter.execute() 返回的 cursor 也支持 lastrowid
# 兼容 KgRepository.insert_entity 用 cur.lastrowid
# psycopg 不像 sqlite3 自动维护 lastrowid；用 RETURNING id 替代
# —— 这块在 KgRepository.insert_entity 直接处理（Repository 已抽象）
