-- Phase 7: chat_models 表 —— 对话模型注册与管理
--
-- 设计要点（与项目其它表一致）：
--   - 用 TEXT model_id（new_id("model")）做业务主键，避免 UUID 扩展依赖
--   - tenant_id 为多租户预留（Phase 8），MVP 全填 1
--   - api_key 明文存储（与 WeKnora 一致）；GET 返回时由 Service 层屏蔽
--   - 软删除：deleted_at 非 NULL = 已删除；正常查询带 WHERE deleted_at IS NULL
--   - is_default：同 tenant 内业务层保证唯一性（不加 unique 索引避免迁移困难）
--
-- 幂等：CREATE TABLE IF NOT EXISTS + 索引 IF NOT EXISTS。

CREATE TABLE IF NOT EXISTS chat_models (
  id            SERIAL PRIMARY KEY,
  model_id      TEXT NOT NULL UNIQUE,
  tenant_id     INTEGER NOT NULL DEFAULT 1,
  name          TEXT NOT NULL,
  provider      TEXT NOT NULL,
  model_name    TEXT NOT NULL,
  base_url      TEXT DEFAULT '',
  api_key       TEXT DEFAULT '',
  is_default    BOOLEAN NOT NULL DEFAULT FALSE,
  enabled       BOOLEAN NOT NULL DEFAULT TRUE,
  description   TEXT DEFAULT '',
  extra_json    TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  deleted_at    TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_models_tenant_enabled
  ON chat_models (tenant_id, enabled)
  WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_chat_models_provider
  ON chat_models (provider)
  WHERE deleted_at IS NULL;
