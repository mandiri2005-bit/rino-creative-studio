-- =====================================================================
-- 0054_narasi_jobs_sweep_stale.sql
-- Dalang v2 Slice 2 (crash-safe billing, fixes H4 "crash delivers free").
--
-- A restart/SIGTERM kills the in-flight _narasi_generate_impl task after
-- chapters are durably persisted but BEFORE the umbrella credit hold settles,
-- leaving the job 'processing' with a live (un-committed) hold → the delivered
-- chapters would be charged 0. This function reaps stale narasi jobs and RETURNS
-- (tenant_id, op_id, meter_actual) — the billing checkpoint the engine stashes in
-- jobs.input_payload._meter — so the sweep loop can COMMIT the delivered cost
-- (credits.commit is idempotent on op_id) or refund when nothing was delivered.
--
-- Cross-tenant (one query over every tenant's stale rows) → SECURITY DEFINER,
-- same precedent as image_jobs_sweep_stale (0048/0049) and the 0046 Clerk helpers:
-- a NOBYPASSRLS role can't UPDATE across tenants under the tenant_isolation policy.
--
-- Only rows carrying a checkpoint (input_payload->'_meter'->>'op_id' present) are
-- swept, so pre-Slice-2 jobs and jobs created while DALANG_CRASHSAFE_ENABLED was
-- OFF are never touched. Forward-only, idempotent (CREATE OR REPLACE). Additive:
-- nothing calls this until the sweep loop is registered under the flag.
-- =====================================================================

BEGIN;

CREATE OR REPLACE FUNCTION narasi_jobs_sweep_stale(p_older_than interval)
RETURNS TABLE (tenant_id UUID, op_id TEXT, meter_actual INT, user_id UUID)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    UPDATE jobs
       SET status = 'error'::job_status_enum,
           error_message = 'swept: orphaned narasi (crash/restart before settle)',
           completed_at = now()
     -- Compare job_type as TEXT (not 'narasi'::job_type_enum): prod's enum has 'narasi' but
     -- the migration chain never ADDs it (out-of-band drift), so the enum literal would fail
     -- CREATE FUNCTION on a fresh migration-built DB. Text compare applies everywhere.
     WHERE job_type::text = 'narasi'
       AND status IN ('processing'::job_status_enum, 'running'::job_status_enum)
       AND updated_at < now() - p_older_than
       AND (input_payload -> '_meter' ->> 'op_id') IS NOT NULL
    RETURNING jobs.tenant_id,
              (jobs.input_payload -> '_meter' ->> 'op_id')::text,
              COALESCE((jobs.input_payload -> '_meter' ->> 'actual')::int, 0),
              jobs.user_id;
$$;

-- Postgres grants EXECUTE to PUBLIC by default; a SECURITY DEFINER cross-tenant
-- writer must be exclusive to the backend runtime role (mirror 0049).
REVOKE ALL ON FUNCTION narasi_jobs_sweep_stale(interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION narasi_jobs_sweep_stale(interval) TO app_user;

COMMIT;
