-- =====================================================================
-- 0022_cleanup_expired_assets.sql
-- Dynamic, tier-based vault retention. Deletes assets older than the owning
-- tenant's retention window (by subscription plan) and RETURNS the deleted
-- object keys so the app can also remove them from R2.
--
-- Retention by plan:  free 7d · starter 14d · pro 30d · enterprise 90d.
-- SECURITY DEFINER so the app role (app_user, NOBYPASSRLS) can run a single
-- cross-tenant sweep; EXECUTE granted to app_user. Idempotent — only ever
-- removes already-expired rows.
-- =====================================================================

BEGIN;

CREATE OR REPLACE FUNCTION cleanup_expired_assets()
RETURNS TABLE(s3_key text, bucket text)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  WITH expired AS (
    SELECT a.id, a.s3_key, a.bucket
      FROM assets a
      LEFT JOIN LATERAL (
        SELECT plan FROM subscriptions s
         WHERE s.tenant_id = a.tenant_id AND s.status IN ('active','trialing')
         LIMIT 1
      ) sub ON true
     WHERE a.is_deleted = false
       AND a.created_at < now() - (CASE COALESCE(sub.plan,'free')
             WHEN 'enterprise' THEN INTERVAL '90 days'
             WHEN 'pro'        THEN INTERVAL '30 days'
             WHEN 'starter'    THEN INTERVAL '14 days'
             ELSE                   INTERVAL '7 days'
           END)
  ), del AS (
    DELETE FROM assets WHERE id IN (SELECT id FROM expired)
    RETURNING s3_key, bucket
  )
  SELECT s3_key, bucket FROM del;
$$;

COMMENT ON FUNCTION cleanup_expired_assets() IS
  'Tier-based vault retention sweep (free 7d/starter 14d/pro 30d/enterprise 90d). Deletes expired asset rows, returns their R2 keys for object cleanup.';

GRANT EXECUTE ON FUNCTION cleanup_expired_assets() TO app_user;

COMMIT;
