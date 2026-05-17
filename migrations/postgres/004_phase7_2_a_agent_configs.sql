-- Phase 7.2.A: agent_configs 表 —— per-agent system_prompt 与对话风格管理
--
-- 背景：Phase 7.1 完成多 provider 真切换后，发现 servers/generation/parameter.yaml 里
-- 的 system_prompt 是 AGV SOP 专用、对所有模型一刀切，限制了 vLLM 等开源模型的
-- 排版自由度。把 prompt 从 yaml 提到 per-agent 行，可在 admin 编辑、立即生效。
--
-- 设计要点（与 chat_models 表风格一致）：
--   - 用 TEXT agent_id（new_id("agent") 或 'builtin-xxx'）做业务主键
--   - tenant_id 为多租户预留，MVP 全填 1
--   - is_builtin=TRUE 的行 admin 可改 prompt 但不可删
--   - model_id 引用 chat_models.model_id 但不加外键约束（chat_models 可能被软删）
--   - agent_mode：'quick' / 'agent'（与现有前端 dropdown 字符串一致）
--   - 软删除：deleted_at IS NULL = 未删除
--
-- 幂等：CREATE TABLE IF NOT EXISTS + 索引 IF NOT EXISTS。

CREATE TABLE IF NOT EXISTS agent_configs (
  id                    SERIAL PRIMARY KEY,
  agent_id              TEXT NOT NULL UNIQUE,
  tenant_id             INTEGER NOT NULL DEFAULT 1,
  name                  TEXT NOT NULL,
  description           TEXT DEFAULT '',
  avatar                TEXT DEFAULT '',
  agent_mode            TEXT NOT NULL,
  is_builtin            BOOLEAN NOT NULL DEFAULT FALSE,
  system_prompt         TEXT DEFAULT '',
  agent_system_prompt   TEXT DEFAULT '',
  model_id              TEXT DEFAULT '',
  temperature           REAL DEFAULT 0.7,
  max_tokens            INTEGER DEFAULT 4096,
  enabled               BOOLEAN NOT NULL DEFAULT TRUE,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  deleted_at            TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_configs_tenant_enabled
  ON agent_configs (tenant_id, enabled)
  WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_agent_configs_model_id
  ON agent_configs (model_id)
  WHERE deleted_at IS NULL;
