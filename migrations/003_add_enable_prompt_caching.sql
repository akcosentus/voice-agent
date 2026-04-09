-- Safety: ensure enable_prompt_caching column exists on agents table.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS enable_prompt_caching boolean NOT NULL DEFAULT true;
