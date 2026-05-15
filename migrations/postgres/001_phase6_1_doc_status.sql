-- Phase 6.1: per-document status tracking
--
-- Adds two columns on kb_documents so each document can carry its own
-- completion timestamp and chunk count. The status enum is extended at the
-- application layer (parsing / embedding / indexing / completed / failed /
-- deleting) - no DB-level CHECK constraint is added so older rows with
-- status='done' continue to load; the repository layer maps 'done' -> 'completed'.
--
-- Idempotent: safe to run multiple times.

ALTER TABLE kb_documents
  ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ;

ALTER TABLE kb_documents
  ADD COLUMN IF NOT EXISTS chunk_count INTEGER NOT NULL DEFAULT 0;
