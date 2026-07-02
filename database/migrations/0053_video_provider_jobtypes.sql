-- =====================================================================
-- 0053_video_provider_jobtypes.sql
-- LAUNCH FIX (surfaced live during the BullMQ video-gen launch, 2026-07-02).
--
-- _persist_asset stamps source_job_type = the WINNING PROVIDER name
-- (laozhang_api.py: `source_job_type=(provider or "video_tools")`), but
-- job_type_enum only carried veo+sora. So EVERY video from any other provider
-- (aiml, fal, kie, atlascloud, laozhang, vertex) FAILED to persist:
--     ERROR insert_asset: invalid input value for enum job_type_enum: "aiml"
--
-- Effect: the video WAS delivered (res.ref from _rehost → R2) and credits WERE
-- charged correctly, but the asset never reached the assets table → it vanished
-- from the user's Vault, and the usage_logs/COGS row was dropped too:
--     ERROR log_usage: violates check constraint "usage_logs_provider_check"
-- Money (credit_ledger) is unaffected — this is data-integrity only.
--
-- ALTER TYPE ... ADD VALUE is proven-safe in this migrate.js (see 0020). The new
-- values are only ADDED here and are NEVER consumed in this file, so it is safe
-- even if migrate.js wraps the file in a transaction (Neon = PG15).
--
-- FOLLOW-UP (code, separate change in laozhang_api.py — NOT this migration):
-- make source_job_type a STABLE value (a single 'video' job type) with the real
-- provider kept in metadata, so a newly-added provider never breaks persist again.
-- =====================================================================

-- (1) register every video-provider name that _persist_asset stamps as source_job_type
ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS 'aiml';
ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS 'fal';
ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS 'kie';
ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS 'atlascloud';
ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS 'laozhang';
ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS 'vertex';
ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS 'video_tools';   -- the `or "video_tools"` fallback

-- (2) usage_logs.provider CHECK was frozen at the 0007 five-value list
--     (laozhang/deepseek/gemini/openai/other) and never widened as the provider
--     roster grew → drop it. It gates ANALYTICS, not money, and the roster changes
--     too often for a hard CHECK to ever stay correct.
ALTER TABLE usage_logs DROP CONSTRAINT IF EXISTS usage_logs_provider_check;
