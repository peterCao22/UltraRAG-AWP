# Phase 2 — 数据模型与 SQLite 设计

> 目标：在不破坏 Phase 1 问答链路的前提下，引入最小可用的元数据数据库能力。  
> 数据库：`db/app.sqlite`

---

## 1. 设计原则

- 先满足单租户可用，再平滑扩展多租户
- 表结构为 Phase 2 基础能力服务，不一次性过度设计
- 所有核心表预留 `tenant_id`、`created_at`、`updated_at`
- 与文件系统共存：DB 管元数据，文档/索引仍存 `data/kb/<kb_id>/...`

---

## 2. 表结构（基础版）

### 2.1 `knowledge_bases`

用途：知识库主表（一个 `kb_id` 对应一个知识库目录）

字段建议：

- `id` INTEGER PRIMARY KEY AUTOINCREMENT
- `kb_id` TEXT NOT NULL UNIQUE
- `name` TEXT NOT NULL
- `description` TEXT DEFAULT ''
- `tenant_id` TEXT NOT NULL DEFAULT 'default'
- `status` TEXT NOT NULL DEFAULT 'active'
  - 枚举：`active` / `disabled` / `archived`
- `data_path` TEXT NOT NULL
  - 例如：`data/kb/agv_demo`
- `index_path` TEXT DEFAULT ''
- `embedding_path` TEXT DEFAULT ''
- `last_indexed_at` TEXT DEFAULT NULL
- `created_at` TEXT NOT NULL
- `updated_at` TEXT NOT NULL

索引建议：

- UNIQUE(`kb_id`)
- INDEX(`tenant_id`, `status`)

---

### 2.2 `kb_documents`

用途：知识库文档记录（原始文档与处理状态）

字段建议：

- `id` INTEGER PRIMARY KEY AUTOINCREMENT
- `kb_id` TEXT NOT NULL
- `tenant_id` TEXT NOT NULL DEFAULT 'default'
- `doc_id` TEXT NOT NULL
  - 业务唯一 ID，可用 UUID
- `file_name` TEXT NOT NULL
- `file_type` TEXT NOT NULL
  - 例如：`docx` / `pdf`
- `file_path` TEXT NOT NULL
- `channel` TEXT NOT NULL DEFAULT 'api'
  - 预留：`api` / `web` / `im`
- `status` TEXT NOT NULL DEFAULT 'pending'
  - 枚举：`pending` / `parsed` / `embedded` / `indexed` / `failed`
- `error_message` TEXT DEFAULT ''
- `created_at` TEXT NOT NULL
- `updated_at` TEXT NOT NULL

约束与索引：

- UNIQUE(`kb_id`, `doc_id`)
- INDEX(`kb_id`, `status`)
- INDEX(`tenant_id`, `kb_id`)

---

### 2.3 `kb_jobs`

用途：入库/索引任务状态追踪

字段建议：

- `id` INTEGER PRIMARY KEY AUTOINCREMENT
- `job_id` TEXT NOT NULL UNIQUE
- `tenant_id` TEXT NOT NULL DEFAULT 'default'
- `kb_id` TEXT NOT NULL
- `job_type` TEXT NOT NULL
  - 枚举：`ingest` / `reindex`
- `status` TEXT NOT NULL DEFAULT 'pending'
  - 枚举：`pending` / `running` / `success` / `failed` / `cancelled`
- `retry_count` INTEGER NOT NULL DEFAULT 0
- `last_error` TEXT DEFAULT ''
- `payload_json` TEXT DEFAULT '{}'
  - 任务参数快照
- `result_json` TEXT DEFAULT '{}'
  - 任务结果快照
- `started_at` TEXT DEFAULT NULL
- `finished_at` TEXT DEFAULT NULL
- `created_at` TEXT NOT NULL
- `updated_at` TEXT NOT NULL

索引建议：

- UNIQUE(`job_id`)
- INDEX(`kb_id`, `status`)
- INDEX(`tenant_id`, `created_at`)

---

### 2.4 `chat_logs`（可选，建议先建）

用途：问答请求审计与后续评估

字段建议：

- `id` INTEGER PRIMARY KEY AUTOINCREMENT
- `tenant_id` TEXT NOT NULL DEFAULT 'default'
- `kb_id` TEXT NOT NULL
- `request_id` TEXT NOT NULL
- `question` TEXT NOT NULL
- `answer` TEXT NOT NULL
- `model_name` TEXT DEFAULT ''
- `top_k` INTEGER DEFAULT 8
- `latency_ms` INTEGER DEFAULT 0
- `source_count` INTEGER DEFAULT 0
- `created_at` TEXT NOT NULL

索引建议：

- INDEX(`kb_id`, `created_at`)
- INDEX(`tenant_id`, `created_at`)
- INDEX(`request_id`)

---

## 3. DDL 草案（SQLite）

```sql
CREATE TABLE IF NOT EXISTS knowledge_bases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kb_id TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  tenant_id TEXT NOT NULL DEFAULT 'default',
  status TEXT NOT NULL DEFAULT 'active',
  data_path TEXT NOT NULL,
  index_path TEXT DEFAULT '',
  embedding_path TEXT DEFAULT '',
  last_indexed_at TEXT DEFAULT NULL,
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

CREATE TABLE IF NOT EXISTS chat_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  kb_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  model_name TEXT DEFAULT '',
  top_k INTEGER DEFAULT 8,
  latency_ms INTEGER DEFAULT 0,
  source_count INTEGER DEFAULT 0,
  created_at TEXT NOT NULL
);
```

---

## 4. 迁移策略

- 迁移方式：`schema_version` + 增量 SQL（`migrations/`）
- 初始版本：`v1`（本文件定义的四表）
- 原则：
  - 禁止破坏性变更直接上线
  - 新增字段优先 `NULL/DEFAULT` 兼容
  - 每次迁移记录执行时间与版本号

---

## 5. 与当前代码的集成点

- API 层：`custom_app/api/kb.py`（下一步）
  - `POST /api/kb` 创建库时写入 `knowledge_bases`
  - `GET /api/kb` 列表查询
- 任务层：`ingest/reindex` 时写 `kb_jobs`
- 问答层：`/api/chat` 可选写 `chat_logs`
- 文件系统：
  - 目录依旧在 `data/kb/<kb_id>/...`
  - DB 仅存路径与状态元数据

---

## 6. 最小验收（数据层）

- 能初始化 `db/app.sqlite`
- 四张表创建成功
- 可插入/查询一个 `knowledge_bases` 记录
- 能写入一条 `kb_jobs` 并完成状态更新
