-- Phase 6.2: per-document scope on KG relations
--
-- 让 kg_relations 行知道自己来自哪个文档，单文件重建 / 删除时只清理该 doc 的关系。
-- 老关系无 doc_id 时为空字符串，按 doc 删除时跳过；用户做一次全量重建即可补齐。
--
-- 幂等：可重复运行。

ALTER TABLE kg_relations
  ADD COLUMN IF NOT EXISTS doc_id TEXT DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_kg_rel_doc
  ON kg_relations (kb_id, doc_id);
