-- =====================================================================
-- 0049_image_jobs_sweep_revoke_public.sql
-- Lock down the 0048 cross-tenant orphan sweep.
--
-- image_jobs_sweep_stale(interval) is SECURITY DEFINER: it runs as its
-- owner (a BYPASSRLS role) and writes across EVERY tenant's rows. Postgres
-- grants EXECUTE on functions to PUBLIC by default, so 0048's explicit
-- `GRANT EXECUTE ... TO app_user` was additive, not exclusive — PUBLIC
-- could still invoke it. Any present-or-future low-privilege role (an anon
-- pooler login, a read-only analytics role, etc.) could therefore trip a
-- cross-tenant UPDATE that bypasses RLS.
--
-- REVOKE it from PUBLIC and re-assert the single intended grant (app_user,
-- the backend runtime role). Forward-only, idempotent, never edits 0048.
-- =====================================================================

BEGIN;

REVOKE ALL ON FUNCTION image_jobs_sweep_stale(interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION image_jobs_sweep_stale(interval) TO app_user;

COMMIT;
