-- =====================================================================
-- 0069_create_narasi_outlines.sql
--
-- narasi_outlines was added ad-hoc on dev/staging Neon branches only (per
-- 0021_force_rls_narasi_outlines.sql's own guard comment: "narasi_outlines
-- exists only on some Neon branches (dev/staging), not main") and was never
-- migrated to production (positive-radiance). save_outline() in database.py
-- has been failing non-fatally on every narasi outline generation as a
-- result ("relation narasi_outlines does not exist") — it's a best-effort
-- MOAT/analytics capture, not the outline actually delivered to the caller,
-- so this has been silent data loss rather than a user-facing failure.
--
-- This migration ALSO absorbs what 0021 was meant to do: 0021 is guarded to
-- skip when the table is absent, and migrate.js tracks each migration as
-- run-once — so once THIS migration creates the table, 0021 will NOT
-- retroactively re-run to force RLS onto it. This migration therefore
-- creates the table with RLS FORCED and WITH CHECK included from the start
-- (0011/0021's hardened pattern), not schema.sql's own inline RLS clause at
-- schema.sql:869,894, which is missing both FORCE and WITH CHECK — the exact
-- fail-open shape 0011 already fixed for every other table.
--
-- Column/PK/index/FK definitions mirror schema.sql:365-377, :549-550, :675,
-- :823-828 exactly. Guarded (IF NOT EXISTS) so this is a clean no-op on any
-- branch where the table already exists (dev/staging) — never touches an
-- existing table's data or constraints there.
-- =====================================================================

BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                 WHERE table_schema = 'public' AND table_name = 'narasi_outlines') THEN

    EXECUTE $t$
      CREATE TABLE public.narasi_outlines (
          id uuid DEFAULT gen_random_uuid() NOT NULL,
          tenant_id uuid NOT NULL,
          user_id uuid,
          topic text,
          style text,
          language text,
          chap_count integer,
          outline_text text,
          chapters jsonb,
          model text,
          created_at timestamp with time zone DEFAULT now() NOT NULL
      )
    $t$;

    EXECUTE 'ALTER TABLE ONLY public.narasi_outlines
               ADD CONSTRAINT narasi_outlines_pkey PRIMARY KEY (id)';

    EXECUTE 'CREATE INDEX idx_narasi_outlines_tenant
               ON public.narasi_outlines USING btree (tenant_id, created_at DESC)';

    EXECUTE 'ALTER TABLE ONLY public.narasi_outlines
               ADD CONSTRAINT narasi_outlines_tenant_id_fkey
               FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE';

    EXECUTE 'ALTER TABLE ONLY public.narasi_outlines
               ADD CONSTRAINT narasi_outlines_user_id_fkey
               FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL';

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
