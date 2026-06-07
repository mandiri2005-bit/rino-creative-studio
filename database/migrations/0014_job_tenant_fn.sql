-- 0014_job_tenant_fn.sql
-- A SECURITY DEFINER helper so background job-status writers (updateJobProgress,
-- completeJob, failJob) can resolve a job's owning tenant WITHOUT a request
-- context. SECURITY DEFINER runs as the function owner (the table owner), which
-- bypasses RLS for this single, safe, by-primary-key lookup only.
--
-- Why this is safe: it returns ONLY the tenant_id for a given job id (no row
-- data), and the caller immediately re-enters normal RLS using that tenant_id.

BEGIN;

CREATE OR REPLACE FUNCTION job_tenant(p_job_id uuid)
RETURNS uuid
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT tenant_id FROM jobs WHERE id = p_job_id;
$$;

-- Restrict who may call it (defense-in-depth). Adjust role name if yours differs.
REVOKE ALL ON FUNCTION job_tenant(uuid) FROM PUBLIC;
-- GRANT EXECUTE ON FUNCTION job_tenant(uuid) TO neondb_owner;  -- already owner; here for non-owner app roles

COMMIT;
