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
              -- Phase 6.1: per-document tracking. ALTER 迁移补在 init_db 末尾。
              processed_at TEXT DEFAULT NULL,
              chunk_count INTEGER NOT NULL DEFAULT 0,
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

            -- Phase 7: 对话模型注册表（与 Postgres awprag 表结构对齐）
            CREATE TABLE IF NOT EXISTS chat_models (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              model_id      TEXT NOT NULL UNIQUE,
              tenant_id     INTEGER NOT NULL DEFAULT 1,
              name          TEXT NOT NULL,
              provider      TEXT NOT NULL,
              model_name    TEXT NOT NULL,
              base_url      TEXT DEFAULT '',
              api_key       TEXT DEFAULT '',
              is_default    INTEGER NOT NULL DEFAULT 0,
              enabled       INTEGER NOT NULL DEFAULT 1,
              description   TEXT DEFAULT '',
              extra_json    TEXT NOT NULL DEFAULT '{}',
              created_at    TEXT NOT NULL,
              updated_at    TEXT NOT NULL,
              deleted_at    TEXT DEFAULT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_chat_models_tenant_enabled
              ON chat_models (tenant_id, enabled);

            CREATE INDEX IF NOT EXISTS idx_chat_models_provider
              ON chat_models (provider);

            -- Phase 7.2.A: per-agent system_prompt 与对话风格管理
            CREATE TABLE IF NOT EXISTS agent_configs (
              id                    INTEGER PRIMARY KEY AUTOINCREMENT,
              agent_id              TEXT NOT NULL UNIQUE,
              tenant_id             INTEGER NOT NULL DEFAULT 1,
              name                  TEXT NOT NULL,
              description           TEXT DEFAULT '',
              avatar                TEXT DEFAULT '',
              agent_mode            TEXT NOT NULL,
              is_builtin            INTEGER NOT NULL DEFAULT 0,
              system_prompt         TEXT DEFAULT '',
              agent_system_prompt   TEXT DEFAULT '',
              model_id              TEXT DEFAULT '',
              temperature           REAL DEFAULT 0.7,
              max_tokens            INTEGER NOT NULL DEFAULT 4096,
              enabled               INTEGER NOT NULL DEFAULT 1,
              created_at            TEXT NOT NULL,
              updated_at            TEXT NOT NULL,
              deleted_at            TEXT DEFAULT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_agent_configs_tenant_enabled
              ON agent_configs (tenant_id, enabled);

            CREATE INDEX IF NOT EXISTS idx_agent_configs_model_id
              ON agent_configs (model_id);

            CREATE TABLE IF NOT EXISTS kg_relations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              kb_id TEXT NOT NULL,
              source_id INTEGER NOT NULL,
              target_id INTEGER NOT NULL,
              relation_type TEXT NOT NULL,
              description TEXT DEFAULT '',
              strength INTEGER DEFAULT 5,
              -- Phase 6.2: per-document scope for single-file reindex/delete.
              -- Old rows missing this column get '' via ALTER 迁移；老关系按 doc 删时会跳过。
              doc_id TEXT DEFAULT '',
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

        # Phase 6.1 迁移：kb_documents 加 processed_at / chunk_count 列。
        # 与 Postgres migrations/postgres/001_phase6_1_doc_status.sql 对齐。
        cur = conn.execute("PRAGMA table_info(kb_documents)")
        doc_cols = {row["name"] for row in cur.fetchall()}
        if "processed_at" not in doc_cols:
            conn.execute(
                "ALTER TABLE kb_documents ADD COLUMN processed_at TEXT DEFAULT NULL"
            )
        if "chunk_count" not in doc_cols:
            conn.execute(
                "ALTER TABLE kb_documents ADD COLUMN chunk_count INTEGER NOT NULL DEFAULT 0"
            )

        # Phase 6.2 迁移：kg_relations 加 doc_id 列，支持单文档级 KG 清理。
        # 与 Postgres migrations/postgres/002_phase6_2_kg_doc_id.sql 对齐。
        cur = conn.execute("PRAGMA table_info(kg_relations)")
        rel_cols = {row["name"] for row in cur.fetchall()}
        if "doc_id" not in rel_cols:
            conn.execute(
                "ALTER TABLE kg_relations ADD COLUMN doc_id TEXT DEFAULT ''"
            )

        # Phase 7.2.A 种子数据：插入两个内置 agent（已存在则跳过）。
        # 与 Postgres 端 apply_phase7_2_a_migration.py 的种子逻辑保持一致。
        _seed_builtin_agents(conn)


_AGV_SOP_SYSTEM_PROMPT = (
    "You are a professional AGV (Automated Guided Vehicle) operations assistant "
    "for SOP-based Q&A.\n"
    "Follow the user message instructions exactly: use only the provided excerpts.\n"
    "Whenever the user's question contains any Chinese characters, you MUST answer "
    "with Simplified Chinese for all narrative and procedural text "
    "(translate English SOP excerpts faithfully). "
    "Do not reply in English to Chinese questions.\n"
    "Never omit procedural steps or safety items from those excerpts "
    "(faithful translation / rephrasing only).\n"
    "If information is missing from the excerpts, state clearly that the documentation "
    "is insufficient — do not fabricate.\n"
)


def _seed_builtin_agents(conn: sqlite3.Connection) -> None:
    """init_db 末尾插入 builtin-quick / builtin-agent；已存在则跳过。

    用户后续可在 admin UI 编辑两条记录的 system_prompt / name 等字段。
    """
    now = now_iso()
    for row in _builtin_agent_seed_rows(now):
        cur = conn.execute(
            "SELECT 1 FROM agent_configs WHERE agent_id = ?",
            (row["agent_id"],),
        )
        if cur.fetchone() is not None:
            continue
        conn.execute(
            "INSERT INTO agent_configs "
            "(agent_id, tenant_id, name, description, avatar, agent_mode, "
            " is_builtin, system_prompt, agent_system_prompt, model_id, "
            " temperature, max_tokens, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["agent_id"], row["tenant_id"], row["name"], row["description"],
                row["avatar"], row["agent_mode"], row["is_builtin"],
                row["system_prompt"], row["agent_system_prompt"], row["model_id"],
                row["temperature"], row["max_tokens"], row["enabled"],
                row["created_at"], row["updated_at"],
            ),
        )


def _builtin_agent_seed_rows(now: str) -> list[dict[str, Any]]:
    """返回两个内置 agent 的字段；Postgres 迁移脚本与 SQLite init_db 共用。"""
    return [
        {
            "agent_id": "builtin-quick",
            "tenant_id": 1,
            "name": "快速问答",
            "description": "基于检索的单轮快速问答，沿用 AGV SOP 风格的 system_prompt。",
            "avatar": "",
            "agent_mode": "quick",
            "is_builtin": 1,
            "system_prompt": _AGV_SOP_SYSTEM_PROMPT,
            "agent_system_prompt": "",
            "model_id": "",
            "temperature": 0.2,
            "max_tokens": 4096,
            "enabled": 1,
            "created_at": now,
            "updated_at": now,
        },
        {
            "agent_id": "builtin-agent",
            "tenant_id": 1,
            "name": "智能推理",
            "description": "ReAct 多轮推理 + 工具调用，使用项目内置 Agent system_prompt。",
            "avatar": "",
            "agent_mode": "agent",
            "is_builtin": 1,
            "system_prompt": "",
            "agent_system_prompt": "",
            "model_id": "",
            "temperature": 0.7,
            "max_tokens": 4096,
            "enabled": 1,
            "created_at": now,
            "updated_at": now,
        },
    ]


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}
