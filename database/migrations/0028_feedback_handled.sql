-- =====================================================================
-- 0028_feedback_handled.sql
-- Adds a "handled" flag (+ timestamp) to feedback so the admin console can
-- mark a submission as triaged. Additive and idempotent — existing rows
-- default to unhandled. RLS + grants are already in place (see 0026_feedback).
-- =====================================================================

BEGIN;

ALTER TABLE feedback ADD COLUMN IF NOT EXISTS handled    boolean     NOT NULL DEFAULT false;
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS handled_at timestamptz;

COMMIT;
