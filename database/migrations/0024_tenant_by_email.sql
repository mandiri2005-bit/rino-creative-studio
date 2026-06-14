-- =====================================================================
-- 0024_tenant_by_email.sql
-- Admin helper: resolve a tenant_id from a user email for manual credit grants
-- (bootstrap has no Stripe self-serve top-up yet). SECURITY DEFINER so the app
-- role (app_user, NOBYPASSRLS) can look across tenants for this one lookup —
-- same pattern as job_tenant_by_task(). Read-only; returns NULL if not found.
-- =====================================================================

BEGIN;

CREATE OR REPLACE FUNCTION tenant_id_by_email(p_email text)
RETURNS uuid
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT tenant_id FROM users
   WHERE lower(email) = lower(p_email)
   ORDER BY created_at
   LIMIT 1
$$;

GRANT EXECUTE ON FUNCTION tenant_id_by_email(text) TO app_user;

COMMIT;
