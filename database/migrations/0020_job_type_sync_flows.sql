-- =====================================================================
-- 0020_job_type_sync_flows.sql
-- Extend job_type_enum so synchronous one-shot AI flows (Whisk, Image
-- Generation, Flow storyboard/images, Script→TTS) also get a jobs row — a
-- complete activity ledger alongside usage_logs (the billing ledger).
--
-- NOTE: ADD VALUE IF NOT EXISTS is idempotent. On PG12+ it runs inside a
-- transaction; the new values just can't be USED in the same txn (we only add
-- them here — job rows are inserted at runtime, after this commits).
-- =====================================================================

BEGIN;

ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS 'generate_image';
ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS 'whisk';
ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS 'flow_storyboard';
ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS 'flow_image';
ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS 'script_tts';

COMMIT;
