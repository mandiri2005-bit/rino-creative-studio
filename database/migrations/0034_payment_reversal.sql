-- =====================================================================
-- 0034_payment_reversal.sql
-- Refund / chargeback support for the Dodo + Midtrans rails.
--
-- A refund/dispute event must REVERSE the credits that were granted, else
-- "pay → get credits → refund → keep credits" is an economic hole. Reversal
-- goes through the SAME credit_apply() (negative delta, reason='refund',
-- idempotent on a per-refund op_id). This migration only adds the resolver the
-- webhook needs to find the ORIGINAL credited row (RLS-bypass, like
-- payment_event_lookup) + an audit timestamp.
--
-- NOTE: 0033 is reserved by the unmerged feat/accounting-foundation branch
-- (0031 + 0033); this rail uses 0034 (gap is harmless — migrate.js applies by
-- sorted filename, no contiguity requirement).
-- =====================================================================

BEGIN;

-- Audit: when the credited grant was reversed (refund/chargeback).
ALTER TABLE payment_events ADD COLUMN IF NOT EXISTS reversed_at TIMESTAMPTZ;
COMMENT ON COLUMN payment_events.reversed_at IS 'Set when a refund/chargeback reversed the granted credits (see credit_ledger reason=refund).';

-- Resolver for refund/chargeback: find the ORIGINAL credited row by
-- idempotency_key (Midtrans = order_id, echoed on the refund notification) OR
-- provider_payment_id (Dodo = data.payment_id, on the refund/dispute event).
-- SECURITY DEFINER (runs as owner / BYPASSRLS) so the unauthenticated webhook
-- can resolve the owning tenant + the exact credits_granted to reverse — same
-- pattern as payment_event_lookup (0032) and 0018's /stream resolver.
CREATE OR REPLACE FUNCTION payment_event_for_reversal(
  p_provider TEXT, p_idempotency_key TEXT, p_provider_payment_id TEXT
) RETURNS TABLE(id UUID, tenant_id UUID, user_id UUID, credits_granted INTEGER, credited BOOLEAN, status TEXT)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public                 -- pinned: no caller-controlled escalation
AS $$
  SELECT id, tenant_id, user_id, credits_granted, credited, status
    FROM public.payment_events
   WHERE provider = p_provider
     AND ( (p_idempotency_key   IS NOT NULL AND idempotency_key   = p_idempotency_key)
        OR (p_provider_payment_id IS NOT NULL AND provider_payment_id = p_provider_payment_id) )
   ORDER BY created_at DESC
   LIMIT 1;
$$;
REVOKE ALL ON FUNCTION payment_event_for_reversal(TEXT, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION payment_event_for_reversal(TEXT, TEXT, TEXT) TO app_user;

-- Reconciliation: list stale PENDING payment events across ALL tenants so the
-- operator job can re-check missed webhooks. SECURITY DEFINER so the
-- (NOBYPASSRLS) app role can scan cross-tenant for this one operational read.
CREATE OR REPLACE FUNCTION stale_pending_payments(p_provider TEXT, p_older_than_minutes INT)
RETURNS TABLE(id UUID, tenant_id UUID, idempotency_key TEXT, provider_payment_id TEXT, plan_key TEXT, created_at TIMESTAMPTZ)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT id, tenant_id, idempotency_key, provider_payment_id, plan_key, created_at
    FROM public.payment_events
   WHERE provider = p_provider AND status = 'pending' AND credited = FALSE
     AND created_at < now() - make_interval(mins => p_older_than_minutes)
   ORDER BY created_at ASC
   LIMIT 500;
$$;
REVOKE ALL ON FUNCTION stale_pending_payments(TEXT, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION stale_pending_payments(TEXT, INT) TO app_user;

COMMIT;
