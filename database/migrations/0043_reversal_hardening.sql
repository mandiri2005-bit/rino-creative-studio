-- =====================================================================
-- 0043_reversal_hardening.sql
-- Harden the refund/chargeback reversal path (closing re-audit, 2026-06-27).
--
-- The 0041 clawback fix made reversal HAPPEN (total balance drops) but it was
-- (a) NOT bucket-aware — a negative credit_apply touches credit_balances.balance
--     only, never topup_balance, so a refunded top-up left topup_balance STALE-HIGH
--     → credit_reset_subscription RESURRECTED the refunded credits every renewal;
-- (b) NOT amount-aware — it always reversed the FULL credits_granted, so a PARTIAL
--     refund over-clawed;
-- (c) NOT idempotent at the PAYMENT level — credited/credits_granted were never
--     decremented, so a second reversal event (2nd partial, or refund-then-dispute)
--     full-clawed AGAIN.
--
-- This migration adds the state needed to fix all three (cumulative reversed_credits
-- cap + per-payment bucket) and a bucket-aware, idempotent credit_reverse_grant().
-- Also adds dodo_subscriptions.pending_plan_key for the deferred-downgrade fix (H).
-- Additive + idempotent; Indonesia (topup_balance always 0) is unaffected.
-- =====================================================================

BEGIN;

-- ── payment_events: cumulative reversal cap + which bucket the grant landed in ──
ALTER TABLE payment_events
    ADD COLUMN IF NOT EXISTS reversed_credits INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS bucket           TEXT;   -- 'sub' | 'topup' | NULL(legacy→treated as sub)

COMMENT ON COLUMN payment_events.reversed_credits IS 'Cumulative credits already reversed for this payment (refunds + chargebacks). Caps total reversal at credits_granted; supports partial refunds.';
COMMENT ON COLUMN payment_events.bucket IS 'Which credit bucket the grant landed in: topup → reversal must decrement topup_balance; sub/NULL → total balance only.';

-- Backfill: already-reversed rows are fully reversed (so a stray re-delivery can't
-- re-claw); tag the bucket from the idempotency_key namespace (topup rows use 'topup:').
UPDATE payment_events
   SET reversed_credits = COALESCE(credits_granted, 0)
 WHERE reversed_at IS NOT NULL AND reversed_credits = 0;
UPDATE payment_events
   SET bucket = CASE WHEN idempotency_key LIKE 'topup:%' THEN 'topup' ELSE 'sub' END
 WHERE bucket IS NULL;

-- ── dodo_subscriptions: track a scheduled (deferred) downgrade target separately ──
-- so plan_key stays = the plan the user effectively HAS this period (tier/UI), while
-- the pending lower plan rides here until the next renewal. Fixes the change-plan
-- direction corruption + can't-cancel bug from overloading plan_key.
ALTER TABLE dodo_subscriptions
    ADD COLUMN IF NOT EXISTS pending_plan_key TEXT;
COMMENT ON COLUMN dodo_subscriptions.pending_plan_key IS 'A deferred downgrade target scheduled to take effect at the next renewal; NULL when none pending. plan_key remains the effective (current-period) plan.';

-- ── Resolver: also return reversed_credits, bucket, amount (for the cap + proportional) ──
-- The return type changed (added OUT columns) so REPLACE can't widen it → DROP first,
-- then re-grant (0034 had REVOKE PUBLIC + GRANT app_user).
DROP FUNCTION IF EXISTS payment_event_for_reversal(TEXT, TEXT, TEXT);
CREATE FUNCTION payment_event_for_reversal(
  p_provider TEXT, p_idempotency_key TEXT, p_provider_payment_id TEXT
) RETURNS TABLE(id UUID, tenant_id UUID, user_id UUID, credits_granted INTEGER, credited BOOLEAN,
                status TEXT, reversed_credits INTEGER, bucket TEXT, amount BIGINT)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT id, tenant_id, user_id, credits_granted, credited, status,
         COALESCE(reversed_credits,0), bucket, amount
    FROM public.payment_events
   WHERE provider = p_provider
     AND ( (p_idempotency_key     IS NOT NULL AND idempotency_key     = p_idempotency_key)
        OR (p_provider_payment_id IS NOT NULL AND provider_payment_id = p_provider_payment_id) )
   ORDER BY created_at DESC
   LIMIT 1;
$$;
REVOKE ALL ON FUNCTION payment_event_for_reversal(TEXT, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION payment_event_for_reversal(TEXT, TEXT, TEXT) TO app_user;

-- ── Bucket-aware, idempotent reversal primitive ───────────────────────────────
-- Negative ledger row (idempotent on (tenant_id, op_id)); decrements credit_balances.
-- For a TOPUP reversal it ALSO decrements topup_balance by the part still present and
-- clears topup_expires_at when the bucket empties — so a refunded top-up is NOT
-- resurrected by the next credit_reset_subscription. For a SUB reversal it reduces the
-- total only (the clamp keeps topup_balance <= balance; sub_balance is generated).
-- balance may go negative (the buyer consumed credits they did not ultimately pay for).
CREATE OR REPLACE FUNCTION credit_reverse_grant(
  p_tenant UUID, p_user UUID, p_credits INTEGER, p_bucket TEXT,
  p_op_id TEXT, p_metadata JSONB DEFAULT '{}'::jsonb
) RETURNS TABLE(applied BOOLEAN, balance BIGINT, delta INTEGER)
LANGUAGE plpgsql AS $$
DECLARE v_ledger_id UUID; v_balance BIGINT;
BEGIN
  IF p_credits IS NULL OR p_credits <= 0 THEN RETURN QUERY SELECT false, 0::BIGINT, 0; RETURN; END IF;
  IF p_op_id IS NULL OR length(p_op_id) = 0 THEN RAISE EXCEPTION 'credit_reverse_grant requires an op_id'; END IF;

  INSERT INTO credit_ledger (tenant_id, user_id, delta, reason, op_id, metadata)
  VALUES (p_tenant, p_user, -p_credits, 'refund', p_op_id,
          COALESCE(p_metadata,'{}'::jsonb) || jsonb_build_object('bucket', COALESCE(p_bucket,'sub')))
  ON CONFLICT (tenant_id, op_id) WHERE op_id IS NOT NULL DO NOTHING
  RETURNING id INTO v_ledger_id;

  IF v_ledger_id IS NULL THEN                              -- replay of the SAME reversal event
    SELECT cb.balance INTO v_balance FROM credit_balances cb WHERE cb.tenant_id = p_tenant;
    RETURN QUERY SELECT false, COALESCE(v_balance, 0::BIGINT), 0; RETURN;
  END IF;

  IF p_bucket = 'topup' THEN
    UPDATE credit_balances cb SET
       topup_balance    = GREATEST(cb.topup_balance - LEAST(p_credits, cb.topup_balance), 0),
       topup_expires_at = CASE WHEN cb.topup_balance - LEAST(p_credits, cb.topup_balance) <= 0
                               THEN NULL ELSE cb.topup_expires_at END,
       balance          = cb.balance - p_credits,
       updated_at       = now()
     WHERE cb.tenant_id = p_tenant
     RETURNING cb.balance INTO v_balance;
  ELSE
    UPDATE credit_balances cb SET
       balance    = cb.balance - p_credits,
       updated_at = now()
     WHERE cb.tenant_id = p_tenant
     RETURNING cb.balance INTO v_balance;
  END IF;

  IF v_balance IS NULL THEN                                -- no balance row yet (shouldn't happen for a real grant)
    INSERT INTO credit_balances (tenant_id, balance) VALUES (p_tenant, -p_credits)
    ON CONFLICT (tenant_id) DO UPDATE SET balance = credit_balances.balance - p_credits, updated_at = now()
    RETURNING credit_balances.balance INTO v_balance;
  END IF;

  UPDATE credit_ledger SET balance_after = v_balance WHERE id = v_ledger_id;
  RETURN QUERY SELECT true, v_balance, -p_credits;
END; $$;

COMMIT;
