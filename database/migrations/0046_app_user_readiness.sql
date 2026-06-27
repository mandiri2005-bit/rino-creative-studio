-- =====================================================================
-- 0046_app_user_readiness.sql
-- Prereq for switching the RUNTIME connection (DATABASE_POOL_URL) from
-- neondb_owner (BYPASSRLS — RLS is a no-op today) to the NOBYPASSRLS
-- `app_user` role so FORCE ROW LEVEL SECURITY is actually enforced.
--
-- Two gaps the app_user audit (2026-06-27) found:
--   (1) credit_reverse_grant (SECURITY INVOKER, defined 0043/0044/0045) has
--       NO explicit GRANT — it relied solely on 0016 ALTER DEFAULT PRIVILEGES.
--       Add an explicit grant so refund/chargeback clawback can never hit
--       "permission denied for function credit_reverse_grant" under app_user.
--   (2) The Clerk identity webhooks (user.updated / user.deleted) update the
--       users table BY external_id with NO tenant context — impossible under
--       RLS for a NOBYPASSRLS role. They are inherently cross-tenant identity
--       ops, so wrap them in SECURITY DEFINER helpers (same pattern as
--       tenant_id_by_email / job_tenant_by_task). Each returns the affected
--       row count for observability.
-- =====================================================================

BEGIN;

-- (1) explicit grant for the one un-granted INVOKER credit function
GRANT EXECUTE ON FUNCTION credit_reverse_grant(UUID,UUID,INTEGER,TEXT,TEXT,JSONB) TO app_user;

-- (2a) Clerk user.updated → sync email by external_id (cross-tenant)
CREATE OR REPLACE FUNCTION clerk_user_update_email(p_external_id text, p_email text)
RETURNS integer
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  WITH upd AS (
    UPDATE users SET email = p_email WHERE external_id = p_external_id RETURNING 1
  ) SELECT COALESCE(count(*),0)::int FROM upd
$$;
REVOKE ALL ON FUNCTION clerk_user_update_email(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clerk_user_update_email(text,text) TO app_user;

-- (2b) Clerk user.deleted → deactivate by external_id (cross-tenant)
CREATE OR REPLACE FUNCTION clerk_user_deactivate(p_external_id text)
RETURNS integer
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  WITH upd AS (
    UPDATE users SET is_active = false WHERE external_id = p_external_id RETURNING 1
  ) SELECT COALESCE(count(*),0)::int FROM upd
$$;
REVOKE ALL ON FUNCTION clerk_user_deactivate(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clerk_user_deactivate(text) TO app_user;

COMMIT;
