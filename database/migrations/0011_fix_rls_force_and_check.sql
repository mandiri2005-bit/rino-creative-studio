-- 0011_fix_rls_force_and_check.sql
-- Corrects the RLS defined in schema.sql, which was fail-open:
--   • no FORCE  → the table-owner role (your app's pooled connection) BYPASSED RLS
--   • no WITH CHECK → tenants could INSERT rows with another tenant_id
--   • USING-only ALL policy → INSERT not guarded
-- This migration makes RLS actually enforce, for SELECT/INSERT/UPDATE/DELETE.

BEGIN;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'users','api_keys','chat_sessions','chat_messages',
    'jobs','usage_logs','assets','subscriptions'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY;', t);
    EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY;', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I;', t);
    EXECUTE format($f$
      CREATE POLICY tenant_isolation ON %I
        USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);
    $f$, t);
  END LOOP;
END $$;

-- tenants: match on id (its own id IS the tenant)
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON tenants;
CREATE POLICY tenant_isolation ON tenants
  USING      (id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK (id = current_setting('app.current_tenant_id', true)::uuid);

COMMIT;
