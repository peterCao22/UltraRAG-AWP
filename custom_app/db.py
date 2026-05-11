import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path("db/app.sqlite")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_bases (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              kb_id TEXT NOT NULL UNIQUE,
              name TEXT NOT NULL,
              description TEXT DEFAULT '',
              tenant_id TEXT NOT NULL DEFAULT 'default',
              status TEXT NOT NULL DEFAULT 'active',
              type TEXT NOT NULL DEFAULT 'sop_docx',
              data_path TEXT NOT NULL,
              index_path TEXT DEFAULT '',
              embedding_path TEXT DEFAULT '',
              last_indexed_at TEXT DEFAULT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kb_jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id TEXT NOT NULL UNIQUE,
              tenant_id TEXT NOT NULL DEFAULT 'default',
              kb_id TEXT NOT NULL,
              job_type TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              retry_count INTEGER NOT NULL DEFAULT 0,
              last_error TEXT DEFAULT '',
              payload_json TEXT DEFAULT '{}',
              result_json TEXT DEFAULT '{}',
              started_at TEXT DEFAULT NULL,
              finished_at TEXT DEFAULT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kb_documents (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              kb_id TEXT NOT NULL,
              tenant_id TEXT NOT NULL DEFAULT 'default',
              doc_id TEXT NOT NULL,
              file_name TEXT NOT NULL,
              file_type TEXT NOT NULL,
              file_path TEXT NOT NULL,
              channel TEXT NOT NULL DEFAULT 'api',
              status TEXT NOT NULL DEFAULT 'pending',
              error_message TEXT DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE (kb_id, doc_id)
            );

            CREATE INDEX IF NOT EXISTS idx_kb_tenant_status
              ON knowledge_bases (tenant_id, status);

            CREATE INDEX IF NOT EXISTS idx_job_kb_status
              ON kb_jobs (kb_id, status);

            CREATE INDEX IF NOT EXISTS idx_doc_kb_status
              ON kb_documents (kb_id, status);

            CREATE TABLE IF NOT EXISTS roles (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              role_id TEXT NOT NULL UNIQUE,
              name TEXT NOT NULL UNIQUE,
              description TEXT DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS role_kb_permissions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              role_id TEXT NOT NULL,
              kb_id TEXT NOT NULL,
              access_level TEXT NOT NULL DEFAULT 'read',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE (role_id, kb_id)
            );

            CREATE INDEX IF NOT EXISTS idx_role_kb_perm
              ON role_kb_permissions (role_id, kb_id);

            CREATE TABLE IF NOT EXISTS kb_sessions (
              session_id TEXT NOT NULL PRIMARY KEY,
              kb_id TEXT NOT NULL,
              title TEXT NOT NULL DEFAULT '',
              agent_mode TEXT NOT NULL DEFAULT 'quick',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_kb_sessions_kb_updated
              ON kb_sessions (kb_id, updated_at);

            CREATE TABLE IF NOT EXISTS kb_session_messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id TEXT NOT NULL,
              role TEXT NOT NULL,
              content TEXT NOT NULL,
              reasoning_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_kb_sess_msg_sid
              ON kb_session_messages (session_id);

            CREATE TABLE IF NOT EXISTS kb_agent_configs (
              kb_id TEXT NOT NULL PRIMARY KEY,
              enabled_tools_json TEXT NOT NULL DEFAULT '[]',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kg_entities (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              kb_id TEXT NOT NULL,
              entity_name TEXT NOT NULL,
              entity_type TEXT NOT NULL,
              description TEXT DEFAULT '',
              chunk_ids TEXT NOT NULL DEFAULT '[]',
              created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_kg_entity_kb ON kg_entities(kb_id);
            CREATE INDEX IF NOT EXISTS idx_kg_entity_name ON kg_entities(entity_name);

            CREATE TABLE IF NOT EXISTS kg_relations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              kb_id TEXT NOT NULL,
              source_id INTEGER NOT NULL,
              target_id INTEGER NOT NULL,
              relation_type TEXT NOT NULL,
              description TEXT DEFAULT '',
              strength INTEGER DEFAULT 5,
              created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_kg_rel_kb ON kg_relations(kb_id);
            CREATE INDEX IF NOT EXISTS idx_kg_rel_source ON kg_relations(source_id);
            CREATE INDEX IF NOT EXISTS idx_kg_rel_target ON kg_relations(target_id);
            """
        )

        # 迁移：旧 DB 没有 reasoning_json 列时补上（CREATE TABLE IF NOT EXISTS 不会改 schema）
        cur = conn.execute("PRAGMA table_info(kb_session_messages)")
        cols = {row["name"] for row in cur.fetchall()}
        if "reasoning_json" not in cols:
            conn.execute(
                "ALTER TABLE kb_session_messages "
                "ADD COLUMN reasoning_json TEXT NOT NULL DEFAULT '{}'"
            )

        # Phase 4 迁移：knowledge_bases 加 type 列（区分 sop_docx / general）
        # 老库无感升级：默认 'sop_docx'，行为完全不变
        cur = conn.execute("PRAGMA table_info(knowledge_bases)")
        kb_cols = {row["name"] for row in cur.fetchall()}
        if "type" not in kb_cols:
            conn.execute(
                "ALTER TABLE knowledge_bases "
                "ADD COLUMN type TEXT NOT NULL DEFAULT 'sop_docx'"
            )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}
