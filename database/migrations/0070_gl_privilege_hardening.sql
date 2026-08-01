-- =====================================================================
-- 0070_gl_privilege_hardening.sql
--
-- Closes §17.2 of WIMBA-ACCOUNTING-TAX-INVESTOR-READINESS-AUDIT-2026-08-01.
--
-- ROOT CAUSE (not a one-off oversight):
--   0016_app_role.sql:27  GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES ... TO app_user
--   0016_app_role.sql:31  ALTER DEFAULT PRIVILEGES ... GRANT ... ON TABLES TO app_user
-- so EVERY owner-created table is auto-granted full DML the moment it is created.
-- A later `GRANT SELECT` is additive and does NOT strip it. 0031:667-672 knew this and
-- REVOKE'd for nine posting tables; two tables were still missed, and one created after
-- 0031 (0044) inherited the same hole. This migration closes the known gaps and ships a
-- detector (database/checks/app_user_privilege_audit.sql) so the NEXT new table cannot
-- reintroduce it silently.
--
-- VERIFIED AGAINST PRODUCTION 2026-08-01 (read-only) before writing:
--   gl_accounts        DELETE,INSERT,SELECT,UPDATE  rls=f  policies=0  tenant_id=no
--   orphan_reversals   DELETE,INSERT,SELECT,UPDATE  rls=f  policies=0  tenant_id=no
--   journal_entries    SELECT                       rls=f  policies=0  tenant_id=YES
--   payments           DELETE,INSERT,SELECT,UPDATE  rls=t  forced=t    policies=1
--
-- EXPLICITLY OUT OF SCOPE — `payments` keeps full DML. That is a DELIBERATE decision at
-- 0031:679-680 ("Tenant-scoped, RLS-confined -> full DML") and production confirms RLS is
-- enabled AND forced with a tenant_isolation policy. It is not a defect and is not touched.
--
-- SAFETY: every REVOKE below was checked against the runtime first. A privilege is only
-- revoked where a fixed-string search over all tracked files found no caller.
--
-- REVERSAL (this repo is forward-only — database/rollback.js is a dev-only full reset, not a
-- per-migration down — so the undo is recorded here rather than shipped as a down-migration):
--     GRANT INSERT, UPDATE, DELETE ON gl_accounts TO app_user;
--     GRANT DELETE ON orphan_reversals TO app_user;
--     ALTER TABLE journal_entries NO FORCE ROW LEVEL SECURITY;
--     ALTER TABLE journal_entries DISABLE ROW LEVEL SECURITY;
--     DROP POLICY IF EXISTS tenant_isolation ON journal_entries;
-- No data is written, altered or deleted by this migration, so reversal is complete and
-- lossless. All four tables held 0 rows in production at the time of writing except
-- gl_accounts (46 reference rows, untouched).
--
-- VERIFICATION PERFORMED (local PostgreSQL 18.4, all 71 migrations applied, not production):
--   * app_user INSERT/UPDATE/DELETE on gl_accounts -> "permission denied"; SELECT still 46 rows
--   * app_user INSERT + UPDATE + SELECT on orphan_reversals still succeed; DELETE denied
--   * with 3 postings (2 tenants + 1 company-level): owner sees 3, app_user sees only its own 1
--   * counterfactual — with RLS disabled, that same app_user sees all 3 including the
--     company-level row, which is the exposure this migration closes
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 1. gl_accounts — chart of accounts. Global reference data.
--
--    app_user currently holds INSERT/UPDATE/DELETE purely by inheritance from 0016;
--    0029:121 only ever asked for SELECT. Runtime writers: NONE — a fixed-string search
--    for 'gl_accounts' over every tracked file returns hits ONLY inside database/migrations.
--    The two consumers (v_revenue_by_gl, and usage_logs.gl_revenue_code tagging) read the
--    code values; nothing writes the table.
--
--    Left writable, an application-role compromise or an injection could rename, delete or
--    re-type an account and silently redirect every future posting — before the posting
--    engine of F-01 even exists. RLS is deliberately NOT enabled: this is global reference
--    data with no tenant_id, so read-only is the correct control, not tenant isolation.
-- ---------------------------------------------------------------------
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON gl_accounts FROM app_user;
GRANT  SELECT ON gl_accounts TO app_user;

COMMENT ON TABLE gl_accounts IS
  'Chart of accounts (created 0029, extended 0031). GLOBAL reference data, no tenant_id. '
  'app_user is READ-ONLY as of 0070 — writes are owner-only. Do not grant DML: a writable '
  'CoA lets application traffic redirect postings.';

-- ---------------------------------------------------------------------
-- 2. orphan_reversals — out-of-order refund queue.
--
--    Cross-tenant BY DESIGN (0044:87: "the refund webhook has no tenant ctx"), so it carries
--    no tenant_id and RLS cannot isolate it. RLS is therefore NOT enabled here — doing so
--    would break the queue rather than protect it.
--
--    But DELETE is not needed. The runtime only ever does:
--      payments_core.mjs:223  SELECT ... FROM orphan_reversals
--      payments_core.mjs:233  UPDATE orphan_reversals SET applied_at=now() WHERE id=$1
--      payments_core.mjs:268  INSERT INTO orphan_reversals (...)
--    Rows are retired by stamping applied_at, never by deletion. The DELETE granted at
--    0044:102 is unused, and it is the one privilege that can destroy refund evidence —
--    which is exactly the financial history F-05 says must not be mutable by the app role.
-- ---------------------------------------------------------------------
REVOKE DELETE, TRUNCATE ON orphan_reversals FROM app_user;
-- (SELECT/INSERT/UPDATE deliberately retained — the reversal queue needs all three.)

COMMENT ON TABLE orphan_reversals IS
  'Out-of-order reversal queue (GLOBAL/cross-tenant by design — the refund webhook carries '
  'no tenant context, so there is no tenant_id and RLS is not applicable). Retired by '
  'stamping applied_at, never by DELETE; app_user lost DELETE in 0070. NOTE: raw_event holds '
  'the full gateway payload — see audit F-06, retention/redaction still outstanding.';

-- ---------------------------------------------------------------------
-- 3. journal_entries — posted ledger header. HAS tenant_id, but RLS was never enabled.
--
--    app_user holds SELECT (granted 0031:701) on a table with a tenant_id column and no
--    row-level policy, so once the F-01 posting engine starts writing, one tenant's
--    connection could read EVERY tenant's journal headers. That is the same exposure that
--    0031's own "FIX #6" guarded against for faktur_pajak (buyer identity / DPP leakage) —
--    the reasoning was simply never carried across to the journal itself.
--
--    Enabling it now is free: the table has 0 rows in production and has no runtime reader
--    or writer. The derive engine posts as the DB OWNER (BYPASSRLS per 0031:663-665), so
--    forcing RLS here constrains application traffic only and cannot block the engine.
--
--    tenant_id IS NULL means a company-level entry (0031:157: "NULL = company-level entry
--    (fx_reval, period close, depreciation)"). Those are deliberately NOT visible to
--    app_user: the predicate below is false for NULL, so company-level postings stay
--    owner-only. Reporting that needs them runs as owner.
-- ---------------------------------------------------------------------
ALTER TABLE journal_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE journal_entries FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON journal_entries;
CREATE POLICY tenant_isolation ON journal_entries
    USING      (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

COMMENT ON COLUMN journal_entries.tenant_id IS
  'Tenant the entry belongs to; NULL = company-level (fx_reval, period close, depreciation). '
  'RLS forced as of 0070: app_user sees only its own tenant, and NEVER company-level rows '
  '(the predicate is false for NULL). The derive engine posts as owner and bypasses this.';

-- ---------------------------------------------------------------------
-- 4. journal_lines — deliberately NOT given RLS.
--
--    It has no tenant_id of its own; its tenant scoping lives on the header via entry_id.
--    Adding a policy would require a correlated subquery on every row read, and the header
--    is now the enforcement point. Documented so the asymmetry is not read as an oversight.
--    Residual exposure to accept knowingly: app_user retains SELECT on journal_lines, so
--    amounts are readable without their header context. Revisit when the posting engine
--    lands and real reporting needs are known.
-- ---------------------------------------------------------------------
COMMENT ON TABLE journal_lines IS
  'Posted ledger lines. No tenant_id — tenant scoping is enforced on journal_entries (RLS '
  'forced, 0070). app_user is SELECT-only. Intentionally has no policy of its own; revisit '
  'when the F-01 derive engine and its reporting surface exist.';

COMMIT;
