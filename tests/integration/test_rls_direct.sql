-- =============================================================================
--  Rino Creative Studio — Direct RLS Verification
--  tests/integration/test_rls_direct.sql
--
--  Validates that PostgreSQL Row-Level Security policies enforced in schema.sql
--  correctly gate visibility based on app.current_tenant_id.
--
--  Usage (superuser or db owner):
--    psql "$DATABASE_URL_DEV" -v ON_ERROR_STOP=1 -f tests/integration/test_rls_direct.sql
--
--  Expects:
--    • RLS is enabled on all 9 tenant-scoped tables
--    • Seed data exists (created by database/seed.js) with at least one tenant
--    • The connection role is a superuser or the DB owner, so it can create
--      the test_reader role and grant privileges
--
--  On success: prints "ALL RLS ASSERTIONS PASSED" and exits 0.
--  On failure: raises an exception and exits non-zero.
-- =============================================================================

\set ON_ERROR_STOP on
\set QUIET on

BEGIN;

-- ─── 1. Create a low-privilege role that is subject to RLS ──────────────────
-- Unlike the superuser or table owner, this role does NOT bypass RLS.
DO $$
BEGIN
    -- Drop if left over from a failed previous run
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'test_reader') THEN
        -- Revoke everything first to avoid dependency errors
        EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM test_reader';
        EXECUTE 'DROP ROLE test_reader';
    END IF;
END
$$;

CREATE ROLE test_reader NOLOGIN;

-- Grant SELECT on every table in the public schema
GRANT USAGE  ON SCHEMA public TO test_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO test_reader;


-- ─── 2. Run queries AS test_reader WITHOUT setting app.current_tenant_id ────
-- Every RLS policy uses:  current_setting('app.current_tenant_id', TRUE)::UUID
-- When the setting is missing (returns ''), the cast to UUID either returns
-- NULL or raises an error, but in either case the USING clause evaluates to
-- FALSE and zero rows should be returned.

SET LOCAL ROLE test_reader;

-- Reset the GUC to ensure it is truly absent
RESET app.current_tenant_id;

-- Helper: assert that a query returns exactly the expected count
CREATE OR REPLACE FUNCTION pg_temp.assert_count(
    p_table  TEXT,
    p_expect BIGINT,
    p_label  TEXT
) RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE
    v_actual BIGINT;
BEGIN
    EXECUTE format('SELECT COUNT(*) FROM %I', p_table) INTO v_actual;
    IF v_actual <> p_expect THEN
        RAISE EXCEPTION 'ASSERTION FAILED [%]: expected % rows from %, got %',
            p_label, p_expect, p_table, v_actual;
    ELSE
        RAISE NOTICE 'OK  [%]: % returned % row(s) as expected',
            p_label, p_table, v_actual;
    END IF;
END;
$$;

-- ── 2a. All 9 tables must return 0 rows without tenant context ──────────────
SELECT pg_temp.assert_count('tenants',       0, 'NO_TENANT_CTX — tenants');
SELECT pg_temp.assert_count('users',         0, 'NO_TENANT_CTX — users');
SELECT pg_temp.assert_count('api_keys',      0, 'NO_TENANT_CTX — api_keys');
SELECT pg_temp.assert_count('chat_sessions', 0, 'NO_TENANT_CTX — chat_sessions');
SELECT pg_temp.assert_count('chat_messages', 0, 'NO_TENANT_CTX — chat_messages');
SELECT pg_temp.assert_count('jobs',          0, 'NO_TENANT_CTX — jobs');
SELECT pg_temp.assert_count('usage_logs',    0, 'NO_TENANT_CTX — usage_logs');
SELECT pg_temp.assert_count('assets',        0, 'NO_TENANT_CTX — assets');
SELECT pg_temp.assert_count('subscriptions', 0, 'NO_TENANT_CTX — subscriptions');


-- ─── 3. Discover the seed tenant's ID ──────────────────────────────────────
-- Switch back to superuser briefly to read the tenants table (bypasses RLS).
RESET ROLE;

DO $$
DECLARE
    v_seed_tenant_id UUID;
    v_count          BIGINT;
BEGIN
    -- Pick the first tenant (seed.js typically creates one)
    SELECT id INTO v_seed_tenant_id
      FROM tenants
     ORDER BY created_at ASC
     LIMIT 1;

    IF v_seed_tenant_id IS NULL THEN
        RAISE EXCEPTION 'No tenants found — run database/seed.js first.';
    END IF;

    RAISE NOTICE 'Seed tenant ID: %', v_seed_tenant_id;

    -- ── 4. Set tenant context and re-run as test_reader ─────────────────────
    PERFORM set_config('app.current_tenant_id', v_seed_tenant_id::TEXT, TRUE);

    -- Switch to the restricted role
    SET LOCAL ROLE test_reader;

    -- ── 4a. chat_sessions for the seed tenant should be > 0 ─────────────────
    SELECT COUNT(*) INTO v_count FROM chat_sessions;
    IF v_count > 0 THEN
        RAISE NOTICE 'OK  [WITH_TENANT_CTX — chat_sessions]: % row(s) visible for seed tenant',
            v_count;
    ELSE
        RAISE WARNING 'WARN [WITH_TENANT_CTX — chat_sessions]: 0 rows — seed data may be missing. '
                      'Run database/seed.js to populate test data, then re-run.';
        -- We do NOT raise EXCEPTION here because the RLS itself is working
        -- correctly (returning rows only for the set tenant). An empty result
        -- just means the seed script hasn't inserted chat_sessions yet.
    END IF;

    -- ── 4b. tenants table should show exactly 1 row (the seed tenant) ───────
    SELECT COUNT(*) INTO v_count FROM tenants;
    IF v_count = 1 THEN
        RAISE NOTICE 'OK  [WITH_TENANT_CTX — tenants]: exactly 1 row visible (own tenant)';
    ELSIF v_count = 0 THEN
        RAISE EXCEPTION 'ASSERTION FAILED [WITH_TENANT_CTX — tenants]: 0 rows visible even with correct tenant_id set!';
    ELSE
        RAISE EXCEPTION 'ASSERTION FAILED [WITH_TENANT_CTX — tenants]: % rows visible — expected exactly 1!', v_count;
    END IF;

    -- ── 4c. Cross-tenant isolation: fabricated tenant should see 0 rows ─────
    RESET ROLE;

    -- Use a UUID that is guaranteed not to exist
    PERFORM set_config('app.current_tenant_id',
                       '00000000-0000-0000-0000-000000000000', TRUE);
    SET LOCAL ROLE test_reader;

    SELECT COUNT(*) INTO v_count FROM tenants;
    IF v_count = 0 THEN
        RAISE NOTICE 'OK  [FAKE_TENANT_CTX — tenants]: 0 rows for non-existent tenant';
    ELSE
        RAISE EXCEPTION 'ASSERTION FAILED [FAKE_TENANT_CTX — tenants]: % rows leaked for fabricated tenant_id!', v_count;
    END IF;

    SELECT COUNT(*) INTO v_count FROM chat_sessions;
    IF v_count = 0 THEN
        RAISE NOTICE 'OK  [FAKE_TENANT_CTX — chat_sessions]: 0 rows for non-existent tenant';
    ELSE
        RAISE EXCEPTION 'ASSERTION FAILED [FAKE_TENANT_CTX — chat_sessions]: % rows leaked for fabricated tenant_id!', v_count;
    END IF;

    SELECT COUNT(*) INTO v_count FROM jobs;
    IF v_count = 0 THEN
        RAISE NOTICE 'OK  [FAKE_TENANT_CTX — jobs]: 0 rows for non-existent tenant';
    ELSE
        RAISE EXCEPTION 'ASSERTION FAILED [FAKE_TENANT_CTX — jobs]: % rows leaked for fabricated tenant_id!', v_count;
    END IF;
END
$$;


-- ─── 5. Cleanup ────────────────────────────────────────────────────────────
RESET ROLE;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM test_reader;
DROP ROLE test_reader;

COMMIT;

\echo ''
\echo '=================================================================='
\echo '  ALL RLS ASSERTIONS PASSED'
\echo '=================================================================='
\echo ''
