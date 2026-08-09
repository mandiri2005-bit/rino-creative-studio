-- =====================================================================
-- app_user_privilege_audit.sql — READ-ONLY detector. Safe against production.
--
-- WHY THIS EXISTS
--   0016_app_role.sql:27  GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES ... TO app_user
--   0016_app_role.sql:31  ALTER DEFAULT PRIVILEGES ... GRANT ... ON TABLES TO app_user
--   Every owner-created table is therefore auto-granted FULL DML at creation. A later
--   `GRANT SELECT` is additive and does NOT strip it — only an explicit REVOKE does.
--   That default is silent and opt-out, so a new table is insecure until someone remembers.
--   It has already bitten three times: gl_accounts (0029), orphan_reversals (0044), and
--   journal_entries left without RLS (0031). All three closed in 0070.
--
--   The default privileges are NOT changed by 0070: flipping them would make every future
--   migration fail closed in ways that are easy to miss at deploy time. The trade chosen
--   instead is to keep the permissive default and DETECT violations here.
--
-- USAGE
--   psql "$DATABASE_URL" -X -f database/checks/app_user_privilege_audit.sql
--   Exit is always 0; read the output. Section A must be empty. Sections B and C are
--   review lists, not automatic failures.
--
-- Run as any role that can read the catalog. Prefer the OWNER: under app_user, FORCE RLS
-- makes tenant-scoped reads fail without an app.current_tenant_id GUC.
-- =====================================================================
\pset pager off

BEGIN READ ONLY;

\echo ''
\echo '=== A. MUST BE EMPTY — app_user can WRITE a table that has no row-level protection ==='
\echo '    (DML granted, RLS off, and no tenant_id column to isolate by)'
SELECT c.relname AS table_name,
       (SELECT string_agg(g.privilege_type, ',' ORDER BY g.privilege_type)
          FROM information_schema.role_table_grants g
         WHERE g.table_schema = 'public'
           AND g.table_name   = c.relname
           AND g.grantee      = 'app_user'
           AND g.privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE')) AS writable_privs
  FROM pg_class c
 WHERE c.relnamespace = 'public'::regnamespace
   AND c.relkind = 'r'
   AND NOT c.relrowsecurity
   AND NOT EXISTS (SELECT 1 FROM information_schema.columns col
                    WHERE col.table_schema = 'public'
                      AND col.table_name   = c.relname
                      AND col.column_name  = 'tenant_id')
   AND EXISTS (SELECT 1 FROM information_schema.role_table_grants g
                WHERE g.table_schema = 'public'
                  AND g.table_name   = c.relname
                  AND g.grantee      = 'app_user'
                  AND g.privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE'))
   -- ACCEPTED EXCEPTIONS. Each one was checked against the runtime: a live caller writes
   -- it, so revoking would break production. They are NOT declared safe — they are declared
   -- known, so that section A stays green and a genuinely NEW hole is impossible to miss.
   -- A gate that is red on day one gets ignored, which is how gl_accounts survived since
   -- 0029. Section A2 keeps them visible; see the decision list there.
   AND c.relname NOT IN (
        'orphan_reversals',          -- cross-tenant BY DESIGN (0044:87); payments_core.mjs:233,268 needs INSERT/UPDATE
        'processed_stripe_events',   -- Stripe webhook idempotency marker; billing.mjs:222 needs INSERT
        'narasi_known_bad_claims',   -- factgate reference; python/database.py:969 needs INSERT
        'narasi_known_good_claims',  -- factgate reference; python/database.py:999 needs INSERT
        'migrations',                -- written by database/migrate.js:115
        -- CR-29, added by 0087. KNOWN, NOT SAFE. Both provider-global (keyed by payment
        -- identity, which predates any tenant), so neither can carry tenant RLS.
        'g3_provider_payments',      -- SELECT/INSERT/UPDATE for g3_pin_payment_at, g3_record_payment_terms,
                                     -- g3_write_lot, g3_post_cash_in (0086); backend/g3_lots.mjs:69,88.
                                     -- DELETE/TRUNCATE revoked by 0087.
        'g3_payment_at_divergence_alerts'  -- INSERT only, written by g3_pin_payment_at (0086). No runtime
                                     -- reader, so 0087 revoked SELECT as well. Append-only trigger kept.
   )
 ORDER BY 1;

\echo ''
\echo '=== A2. ACCEPTED EXCEPTIONS — writable + unprotected, each with a live caller ==='
\echo '    Not safe, just known. Every row here is an open decision, not a closed one:'
\echo '      processed_stripe_events  needs INSERT, but DELETE lets the app erase an'
\echo '                               idempotency marker — relevant to audit F-10. Stripe is'
\echo '                               not configured today, so this is latent.'
\echo '      narasi_known_*_claims    app-writable factgate reference data: a compromised app'
\echo '                               role could poison fact-checking for every tenant.'
\echo '      migrations               app role can rewrite migration history.'
\echo '      orphan_reversals         genuinely cannot use RLS (no tenant context by design);'
\echo '                               DELETE/TRUNCATE already revoked in 0070.'
\echo '      g3_provider_payments     provider-global payment aggregate (CR-29). No tenant_id exists'
\echo '                               to key a policy on, so tenant RLS is impossible rather than'
\echo '                               merely absent. 0087 narrowed it to SELECT/INSERT/UPDATE.'
\echo '      g3_payment_at_divergence_alerts  provider-global append-only audit (CR-29). 0087 left'
\echo '                               app_user with INSERT only; no runtime reader exists.'
\echo '    None is in the scope of audit 17.2 (the GL surface). Narrowing them is separate work.'
SELECT c.relname AS table_name,
       (SELECT string_agg(g.privilege_type, ',' ORDER BY g.privilege_type)
          FROM information_schema.role_table_grants g
         WHERE g.table_schema = 'public' AND g.table_name = c.relname
           AND g.grantee = 'app_user') AS app_user_privs
  FROM pg_class c
 WHERE c.relnamespace = 'public'::regnamespace
   AND c.relkind = 'r'
   AND c.relname IN ('orphan_reversals','processed_stripe_events','narasi_known_bad_claims',
                     'narasi_known_good_claims','migrations',
                     'g3_provider_payments','g3_payment_at_divergence_alerts')
 ORDER BY 1;

\echo ''
\echo '=== B. REVIEW — tenant-scoped table with RLS OFF (cross-tenant read/write exposure) ==='
\echo '    (has a tenant_id column, app_user has some grant, but no row-level security)'
SELECT c.relname AS table_name,
       c.relrowsecurity  AS rls_enabled,
       c.relforcerowsecurity AS rls_forced,
       (SELECT count(*) FROM pg_policies p
         WHERE p.schemaname = 'public' AND p.tablename = c.relname) AS policies,
       (SELECT string_agg(g.privilege_type, ',' ORDER BY g.privilege_type)
          FROM information_schema.role_table_grants g
         WHERE g.table_schema = 'public' AND g.table_name = c.relname
           AND g.grantee = 'app_user') AS app_user_privs
  FROM pg_class c
 WHERE c.relnamespace = 'public'::regnamespace
   AND c.relkind = 'r'
   AND EXISTS (SELECT 1 FROM information_schema.columns col
                WHERE col.table_schema = 'public'
                  AND col.table_name   = c.relname
                  AND col.column_name  = 'tenant_id')
   AND NOT (c.relrowsecurity AND c.relforcerowsecurity)
   AND EXISTS (SELECT 1 FROM information_schema.role_table_grants g
                WHERE g.table_schema = 'public'
                  AND g.table_name   = c.relname AND g.grantee = 'app_user')
 ORDER BY 1;

\echo ''
\echo '=== C. REVIEW — full privilege map for the accounting + money surface ==='
SELECT c.relname AS table_name,
       COALESCE((SELECT string_agg(g.privilege_type, ',' ORDER BY g.privilege_type)
                   FROM information_schema.role_table_grants g
                  WHERE g.table_schema = 'public' AND g.table_name = c.relname
                    AND g.grantee = 'app_user'), '(no grant)') AS app_user,
       c.relrowsecurity AS rls, c.relforcerowsecurity AS forced,
       (SELECT count(*) FROM pg_policies p
         WHERE p.schemaname = 'public' AND p.tablename = c.relname) AS policies
  FROM pg_class c
 WHERE c.relnamespace = 'public'::regnamespace
   AND c.relkind = 'r'
   AND c.relname IN ('gl_accounts','journal_entries','journal_lines','accounting_periods',
                     'tax_rates','faktur_pajak','wht_ledger','provider_invoices',
                     'deferred_revenue_snapshots','fx_rates','payments','credit_lots',
                     'credit_ledger','credit_balances','payment_events','free_grants',
                     'orphan_reversals','usage_logs','subscriptions','dodo_subscriptions')
 ORDER BY 1;

\echo ''
\echo '=== D. deferred-revenue completeness (audit 17.1) — is_complete must be true ==='
\echo '    false means credits exist on a plan with no IDR price; the figure is unusable.'
SELECT * FROM v_deferred_revenue;

-- =====================================================================
-- E–H. L2B-METER platform-QC surface (0074)
--
-- The 0016 default-privileges trap has THREE halves, and 0074 hit all three on its first
-- executed run. Sections E–G detect each independently; a comment in the migration does not.
--   0016:27  GRANT EXECUTE ON ALL FUNCTIONS ... TO app_user
--   0016:31  ALTER DEFAULT PRIVILEGES ... GRANT ... ON TABLES    TO app_user
--   0016:34  ALTER DEFAULT PRIVILEGES ... GRANT ... ON SEQUENCES TO app_user
--   0016:36  ALTER DEFAULT PRIVILEGES ... GRANT EXECUTE ON FUNCTIONS TO app_user
-- Measured on the first run of 0074 before the fix: app_user held EXECUTE on SIX QC
-- functions (including the append-only trigger function) instead of five, and the trigger
-- function still carried its default PUBLIC grant.
-- =====================================================================

\echo ''
\echo '=== E. MUST BE EMPTY — app_user holds ANY privilege on a platform-QC table ==='
\echo '    app_user must hold NOTHING on these, not even SELECT: reading is owner-only'
\echo '    (D-METER-17) and writing goes through SECURITY DEFINER functions.'
SELECT c.relname AS table_name,
       string_agg(a.privilege_type, ',' ORDER BY a.privilege_type) AS app_user_privs
  FROM pg_class c, aclexplode(c.relacl) a
 WHERE c.relnamespace = 'public'::regnamespace
   AND c.relname IN ('platform_qc_usage','platform_qc_kill','v_platform_qc_cost')
   AND a.grantee = 'app_user'::regrole
 GROUP BY 1 ORDER BY 1;

\echo ''
\echo '=== F. MUST BE EMPTY — unexpected EXECUTE grant on a platform_qc% function ==='
\echo '    Expected exactly: app_user -> begin_attempt, finish_attempt, resolve_cost,'
\echo '    arm_kill, kill_is_armed (5). platform_qc_reaper -> reap, kill_sync (2).'
\echo '    PUBLIC -> none. The append-only TRIGGER function must hold no non-owner grant.'
\echo '    Owner/superuser execution is NOT a finding (D-METER-28).'
SELECT p.proname AS function_name,
       COALESCE(pg_get_userbyid(NULLIF(a.grantee, 0)), 'PUBLIC') AS grantee
  FROM pg_proc p, aclexplode(p.proacl) a
 WHERE p.pronamespace = 'public'::regnamespace
   AND p.proname LIKE 'platform\_qc%'
   AND a.privilege_type = 'EXECUTE'
   AND a.grantee <> (SELECT oid FROM pg_roles WHERE rolname = current_user)
   AND a.grantee IS DISTINCT FROM (SELECT relowner FROM pg_class WHERE relname = 'platform_qc_usage')
   AND NOT (
        (a.grantee = 'app_user'::regrole AND p.proname IN
            ('platform_qc_begin_attempt','platform_qc_finish_attempt',
             'platform_qc_resolve_cost','platform_qc_arm_kill','platform_qc_kill_is_armed'))
     OR (a.grantee = 'platform_qc_reaper'::regrole AND p.proname IN
            ('platform_qc_reap','platform_qc_kill_sync'))
   )
 ORDER BY 1,2;

\echo ''
\echo '=== F2. REVIEW — the QC function grant surface, counted ==='
\echo '    app_user must read 5, platform_qc_reaper 2, PUBLIC 0.'
SELECT COALESCE(pg_get_userbyid(NULLIF(a.grantee, 0)), 'PUBLIC') AS grantee,
       count(*) AS execute_grants,
       string_agg(p.proname, ', ' ORDER BY p.proname) AS functions
  FROM pg_proc p, aclexplode(p.proacl) a
 WHERE p.pronamespace = 'public'::regnamespace
   AND p.proname LIKE 'platform\_qc%'
   AND a.privilege_type = 'EXECUTE'
   AND a.grantee IS DISTINCT FROM (SELECT relowner FROM pg_class WHERE relname = 'platform_qc_usage')
 GROUP BY 1 ORDER BY 1;

\echo ''
\echo '=== G. MUST BE EMPTY — app_user holds a grant on the QC identity sequence ==='
SELECT c.relname AS sequence_name,
       string_agg(a.privilege_type, ',' ORDER BY a.privilege_type) AS app_user_privs
  FROM pg_class c, aclexplode(c.relacl) a
 WHERE c.relnamespace = 'public'::regnamespace
   AND c.relkind = 'S'
   AND c.relname LIKE 'platform\_qc%'
   AND a.grantee = 'app_user'::regrole
 GROUP BY 1 ORDER BY 1;

\echo ''
\echo '=== H. reaper role — NOBYPASSRLS, schema USAGE required, zero table privileges ==='
\echo '    rolbypassrls MUST be false: neondb_owner carries it, so FORCE RLS is not a'
\echo '    protection against the owner and a scheduled unattended job must not inherit it.'
\echo '    has_schema_usage MUST be true — without it the two EXECUTE grants are unreachable.'
SELECT r.rolname, r.rolsuper, r.rolbypassrls, r.rolcanlogin,
       has_schema_privilege(r.rolname, 'public', 'USAGE') AS has_schema_usage,
       (SELECT count(*) FROM pg_class c, aclexplode(c.relacl) a
         WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'
           AND a.grantee = r.oid)                        AS table_grants_must_be_0
  FROM pg_roles r WHERE r.rolname = 'platform_qc_reaper';

\echo ''
\echo '=== I. REVIEW — QC tables must be RLS-enabled AND forced ==='
SELECT c.relname, c.relrowsecurity AS rls, c.relforcerowsecurity AS forced,
       (SELECT count(*) FROM pg_policies p
         WHERE p.schemaname='public' AND p.tablename=c.relname) AS policies
  FROM pg_class c
 WHERE c.relnamespace='public'::regnamespace
   AND c.relname IN ('platform_qc_usage','platform_qc_kill')
 ORDER BY 1;

COMMIT;
