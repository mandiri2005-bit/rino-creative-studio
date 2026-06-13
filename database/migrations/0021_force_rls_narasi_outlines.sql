-- =====================================================================
-- 0021_force_rls_narasi_outlines.sql
-- narasi_outlines had RLS ENABLED but not FORCED — the same fail-open hole
-- 0011 fixed for the other tables (this table was added ad-hoc on some branches
-- and missed the sweep). FORCE it so the policy also applies to the table owner.
--
-- GUARDED: narasi_outlines exists only on some Neon branches (dev/staging), not
-- main — so skip cleanly when it's absent instead of failing the pipeline.
-- =====================================================================

BEGIN;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'narasi_outlines') THEN
    EXECUTE 'ALTER TABLE narasi_outlines ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE narasi_outlines FORCE  ROW LEVEL SECURITY';
    EXECUTE 'DROP POLICY IF EXISTS tenant_isolation ON narasi_outlines';
    EXECUTE $p$
      CREATE POLICY tenant_isolation ON narasi_outlines
        USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    $p$;
  END IF;
END $$;

COMMIT;
