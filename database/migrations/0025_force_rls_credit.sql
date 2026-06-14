-- =====================================================================
-- 0025_force_rls_credit.sql
-- Harden the Step 4 money tables to the codebase RLS standard.
-- 0023 only ENABLEd RLS on credit_balances / credit_ledger; every other
-- tenant-scoped table is also FORCE'd (0011/0013/0017/0021) so the table-OWNER
-- role cannot bypass its own policies (fail-closed). Add the missing FORCE here.
-- Idempotent. processed_stripe_events stays non-RLS (no tenant_id, written by
-- the webhook before a tenant context exists).
-- =====================================================================

BEGIN;

ALTER TABLE credit_balances FORCE ROW LEVEL SECURITY;
ALTER TABLE credit_ledger   FORCE ROW LEVEL SECURITY;

COMMIT;
