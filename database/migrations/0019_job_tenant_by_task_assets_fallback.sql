-- =====================================================================
-- 0019_job_tenant_by_task_assets_fallback.sql
-- Step 2 — make Veo/Sora video durability survive job cleanup.
--
-- cleanup_old_jobs() DELETEs done/error jobs after 24h. Once a veo/sora job is
-- marked 'done' it is purged, so the jobs-only lookup in 0018 would return NULL
-- and /stream could no longer resolve the tenant to serve the video from R2.
-- The captured `assets` row is NOT purged (it records task_id in metadata), so
-- fall back to it. Keeps video durable indefinitely, not just for 24h.
-- =====================================================================

BEGIN;

CREATE OR REPLACE FUNCTION job_tenant_by_task(p_task_id text)
RETURNS uuid
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(
    -- fast path: the (possibly recent) job row
    (SELECT tenant_id FROM jobs
       WHERE result_payload->>'task_id' = p_task_id
         AND job_type IN ('veo','sora')
       ORDER BY created_at DESC LIMIT 1),
    -- durable fallback: the captured asset (survives cleanup_old_jobs)
    (SELECT tenant_id FROM assets
       WHERE asset_type = 'video'
         AND metadata->>'task_id' = p_task_id
       ORDER BY created_at DESC LIMIT 1)
  )
$$;

GRANT EXECUTE ON FUNCTION job_tenant_by_task(text) TO app_user;

COMMIT;
