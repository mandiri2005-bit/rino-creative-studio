-- =====================================================================
-- 0087_cr29_g3_privilege_and_rls_hardening.sql
--
-- Closes the privilege hole that `0086` opened and that CI caught.
--
-- WHAT WENT WRONG. `0016_app_role.sql:31` sets
-- `ALTER DEFAULT PRIVILEGES ... GRANT ... ON TABLES TO app_user`, so EVERY new
-- table is created with full DML for `app_user` unless a migration says
-- otherwise. `0086` created four tables and said nothing, so all four inherited
-- INSERT/UPDATE/DELETE/SELECT. Three of them have no `tenant_id` and no RLS, so
-- `tests/python/test_accounting_privileges.py::test_live_no_writable_unprotected_table`
-- failed — correctly. The fourth, `g3_topup_quarantine`, escaped that query only
-- because it happens to carry a `tenant_id` column; it had no RLS either, which
-- is the more serious of the two problems and is fixed here.
--
-- 🔴 FORWARD-ONLY, NOT AN EDIT TO 0086. `0086` is unapplied in production but is
-- already applied in CI and in local clones, and `migrate.js` tracks by filename,
-- so editing it would silently skip every environment that already ran it. That
-- is exactly the rule this project holds; hence a new number.
--
-- DEPLOY ORDER: irrelevant in isolation — `0087` only tightens privileges on
-- objects `0086` creates, so the two apply back-to-back in one migrator run.
-- Neither may be applied while `G3_LOT_WRITER_ENABLED` is on.
-- =====================================================================

BEGIN;

DO $guard$
BEGIN
    IF to_regclass('public.g3_provider_payments') IS NULL
       OR to_regclass('public.g3_topup_quarantine') IS NULL
       OR to_regclass('public.g3_payment_at_divergence_alerts') IS NULL
       OR to_regclass('public.g3_cr29_workaround_window') IS NULL THEN
        RAISE EXCEPTION 'ABORT 0087: migration 0086 objects are absent; apply 0086 first.';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        RAISE EXCEPTION 'ABORT 0087: role app_user does not exist; apply 0016 first.';
    END IF;
END
$guard$;

-- =====================================================================
-- 1. g3_cr29_workaround_window — READ ONLY to the app.
--
--    The runtime only ever READS this: g3_write_lot selects started_at/ended_at
--    to decide whether the workaround is active. The single row is seeded by
--    0086, and opening/closing the window is a deliberate operator act, not
--    something the application may do to itself.
-- =====================================================================
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON g3_cr29_workaround_window FROM app_user;
GRANT  SELECT ON g3_cr29_workaround_window TO app_user;

COMMENT ON TABLE g3_cr29_workaround_window IS
  'CR-29 temporary-workaround active window. Cohort membership is granted_at within [started_at, ended_at) -- NOT grandfather_reason, which Decision 1 made a why-marker rather than a cohort marker. Singleton; started_at immutable; ended_at may be set exactly once. 0087: READ-ONLY to app_user -- opening or closing the window is an operator act.';

-- =====================================================================
-- 2. g3_provider_payments — narrowed, and declared a KNOWN EXCEPTION.
--
--    🔴 This table is NOT safe. It is provider-global BY DESIGN: its key is
--    (provider, provider_payment_id), payment identity, which exists before and
--    independently of any tenant. It therefore has no tenant_id and CANNOT carry
--    tenant RLS. A permissive `USING (true)` policy would make the audit query
--    green while protecting nothing, which is worse than the honest exception --
--    so none is added.
--
--    Live callers, all SECURITY INVOKER (they run as app_user):
--      g3_pin_payment_at        INSERT ... ON CONFLICT DO NOTHING, SELECT FOR UPDATE, UPDATE  (0086)
--      g3_record_payment_terms  INSERT ... ON CONFLICT DO NOTHING, UPDATE                     (0086)
--      g3_write_lot             SELECT                                                        (0086)
--      g3_post_cash_in          SELECT                                                        (0086)
--    Reached from backend/g3_lots.mjs:69 (pinPaymentAt) and :88 (recordPaymentTerms).
--
--    Nothing deletes a payment aggregate; a payment that happened cannot un-happen.
-- =====================================================================
REVOKE DELETE, TRUNCATE ON g3_provider_payments FROM app_user;
GRANT  SELECT, INSERT, UPDATE ON g3_provider_payments TO app_user;

COMMENT ON TABLE g3_provider_payments IS
  'CR-29 provider-global payment aggregate, keyed by PAYMENT identity (provider, provider_payment_id) -- never by receipt identity (provider, webhook_id). Carries the temporary business timestamp payment_at and the Decision 3 supplier-fee anchor. 0087: KNOWN UNPROTECTED EXCEPTION -- provider-global by design, so no tenant RLS is possible; DELETE/TRUNCATE revoked, SELECT/INSERT/UPDATE retained for the SECURITY INVOKER functions above. Declared known, NOT declared safe.';

-- =====================================================================
-- 3. g3_payment_at_divergence_alerts — INSERT ONLY.
--
--    Live caller: g3_pin_payment_at (0086) inserts one row when a redelivery
--    carries a different data.created_at. There is NO runtime reader -- verified
--    by search across backend/ and python/ -- so SELECT is revoked too rather
--    than retained on the assumption someone might want it later. Operators read
--    it as an owner role.
--
--    UPDATE/DELETE were already impossible through the append-only trigger from
--    0086; revoking them makes that structural rather than trigger-dependent.
-- =====================================================================
REVOKE SELECT, UPDATE, DELETE, TRUNCATE ON g3_payment_at_divergence_alerts FROM app_user;
GRANT  INSERT ON g3_payment_at_divergence_alerts TO app_user;

COMMENT ON TABLE g3_payment_at_divergence_alerts IS
  'Append-only audit of redeliveries whose data.created_at differs from the pinned payment_at. ALERT ONLY: never overwrites the pin, never quarantines the payment (MATRIX-045 T79). 0087: KNOWN UNPROTECTED EXCEPTION -- provider-global, INSERT-only for app_user (g3_pin_payment_at); SELECT revoked, no runtime reader exists. The 0086 append-only trigger is retained.';

-- =====================================================================
-- 4. g3_topup_quarantine — REAL tenant RLS. This one CAN be protected, so it is.
--
--    It carries tenant_id, which is why the CI query skipped it and why the hole
--    was the quietest of the four: app_user had full DML on a tenant-scoped table
--    with no row-level protection at all — cross-tenant read AND write.
--
--    Policy shape matches the other 25 tenant_isolation policies in this schema
--    exactly, including `missing_ok = true`. 🔴 Keep the known consequence in
--    mind when reading counts: with the GUC UNSET, current_setting(...) is NULL,
--    `tenant_id = NULL` matches nothing, and a count comes back 0 rather than
--    raising — silence that is indistinguishable from an empty table. Never
--    report such a zero as evidence of absence.
--
--    A NULL tenant_id row is unreachable and uninsertable through this policy
--    (NULL = x is never true), which is why backend/g3_lots.mjs now rejects a
--    missing tenantId at the boundary instead of writing a row nobody can see.
-- =====================================================================
ALTER TABLE g3_topup_quarantine ENABLE ROW LEVEL SECURITY;
ALTER TABLE g3_topup_quarantine FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON g3_topup_quarantine;
CREATE POLICY tenant_isolation ON g3_topup_quarantine
    USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

REVOKE DELETE, TRUNCATE ON g3_topup_quarantine FROM app_user;
GRANT  SELECT, INSERT, UPDATE ON g3_topup_quarantine TO app_user;

COMMENT ON TABLE g3_topup_quarantine IS
  'Post-boundary top-up payments whose ToS metadata was absent or unresolvable. No lot is written and no version is guessed; the payment is NEVER forfeited. Downstream completeness signals must report a quarantined payment as incomplete indefinitely. Release is a manual per-row disposition (g3_release_quarantined_topup). 0087: tenant RLS ENABLED + FORCED with tenant_isolation on both USING and WITH CHECK; DELETE/TRUNCATE revoked.';

-- =====================================================================
-- 5. Trigger-only functions must not be callable by the app.
--
--    0016 also sets ALTER DEFAULT PRIVILEGES ... GRANT EXECUTE ON FUNCTIONS TO
--    app_user, so these two picked up EXECUTE they have no business holding.
--    They are trigger bodies: PostgreSQL invokes them through the trigger
--    regardless of EXECUTE, so revoking costs the runtime nothing.
--
--    The other 0086 functions (g3_pin_payment_at, g3_record_payment_terms,
--    g3_write_lot, g3_release_quarantined_topup, g3_post_*) are called directly
--    by the backend and KEEP their EXECUTE deliberately.
-- =====================================================================
REVOKE ALL ON FUNCTION public.g3_pp_payment_at_immutable() FROM PUBLIC, app_user;
REVOKE ALL ON FUNCTION public.g3_cr29_window_guard()       FROM PUBLIC, app_user;

COMMIT;
