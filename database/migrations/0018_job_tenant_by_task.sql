-- =====================================================================
-- 0018_job_tenant_by_task.sql
-- Step 2 (object storage) — Veo/Sora capture support.
--
-- The Veo/Sora MP4 only materialises at /stream time, which is hit by the
-- browser as a <video src> and therefore carries NO Authorization header — so
-- the endpoint has no tenant context. To attach the saved video to the right
-- tenant in `assets`, /veo/submit & /sora/submit (now optionally authenticated)
-- record a jobs row holding the upstream provider task_id in result_payload.
-- This SECURITY DEFINER function lets the unauthenticated /stream endpoint
-- resolve that tenant by task_id, bypassing RLS for this single keyed lookup —
-- exactly the pattern of job_tenant() in 0014.
-- =====================================================================

BEGIN;

CREATE OR REPLACE FUNCTION job_tenant_by_task(p_task_id text)
RETURNS uuid
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT tenant_id
    FROM jobs
   WHERE result_payload->>'task_id' = p_task_id
     AND job_type IN ('veo','sora')
   ORDER BY created_at DESC
   LIMIT 1
$$;

COMMENT ON FUNCTION job_tenant_by_task(text) IS
  'Resolve the owning tenant of a Veo/Sora job by upstream provider task_id. SECURITY DEFINER so the unauthenticated /stream endpoint can attach saved videos to assets under RLS.';

-- app_user runs the app under NOBYPASSRLS; it must be able to call this fn.
GRANT EXECUTE ON FUNCTION job_tenant_by_task(text) TO app_user;

-- Speeds up the lookup (and the existing batch findJobByJobName path) by
-- indexing the task_id pulled out of result_payload for video jobs.
CREATE INDEX IF NOT EXISTS idx_jobs_task_id
    ON jobs ((result_payload->>'task_id'))
    WHERE result_payload->>'task_id' IS NOT NULL;

COMMIT;
