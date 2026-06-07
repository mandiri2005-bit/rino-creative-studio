-- 0016_app_role.sql
-- ROOT CAUSE FIX: the app was connecting as neondb_owner, which has BYPASSRLS
-- and therefore ignores every RLS policy. This creates a dedicated application
-- role WITHOUT BYPASSRLS so RLS actually enforces for app traffic. Migrations
-- and admin tasks continue to run as neondb_owner (which still bypasses RLS —
-- correct, since migrations must see everything).
--
-- Also removes the leftover 'superuser_bypass' and duplicate 'tenant_isolation'
-- policies from the earlier 0011_enable_rls.sql so each table has exactly one
-- clean policy set.

BEGIN;

-- 1. Create the application role (no login yet; we set a password below).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    CREATE ROLE app_user LOGIN;
  END IF;
END $$;

-- Explicitly ensure it does NOT bypass RLS (it won't by default, but be sure).
ALTER ROLE app_user NOBYPASSRLS;

-- 2. Grant it the data privileges it needs (DML only — no DDL, no superuser).
GRANT USAGE ON SCHEMA public TO app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO app_user;
-- Future tables/sequences created by the owner are auto-granted to app_user:
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT EXECUTE ON FUNCTIONS TO app_user;

-- 3. Remove leftover/duplicate policies from the older 0011_enable_rls.sql.
--    (The clean per-command + ALL policies from 0011_fix remain.)
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'tenants','users','api_keys','chat_sessions','chat_messages',
    'jobs','usage_logs','assets','subscriptions','moat_sessions','correction_pairs'
  ] LOOP
    EXECUTE format('DROP POLICY IF EXISTS superuser_bypass ON %I;', t);
    -- keep ONE canonical ALL policy named tenant_isolation; drop the per-command
    -- duplicates so there is no ambiguity. (The ALL policy covers r/a/w/d.)
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation_select ON %I;', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation_insert ON %I;', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation_update ON %I;', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation_delete ON %I;', t);
  END LOOP;
END $$;

COMMIT;
