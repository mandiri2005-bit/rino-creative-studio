-- =====================================================================
-- 0044_reversal_hardening_2.sql
-- Final-audit (round-3) fixes for the reversal path.
--
-- (1) credit_reverse_grant SUB branch was reducing total `balance` only; the
--     clamp_topup_balance BEFORE-trigger then forced topup_balance := LEAST(topup,
--     balance), so a SUB refund that pushed balance below the surviving topup bucket
--     SILENTLY DESTROYED the user's separately-paid, UNREFUNDED top-up credits
--     (reproduced). Fix: a SUB reversal claws only the SUB portion (balance −
--     topup_balance) so balance can never drop below topup_balance → the clamp is a
--     no-op and the paid top-up survives. Each branch now returns the ACTUAL clawed
--     delta (sub may claw less than requested when the sub bucket is already spent).
--
-- (2) orphan_reversals: a refund/dispute whose original charge row isn't recorded yet
--     (out-of-order) used to THROW → webhook 500 → Dodo retry, but retries reuse the
--     ORIGINAL signed timestamp so the ±5-min freshness check rejects them after 5 min
--     → the clawback was permanently lost. Instead we now queue the reversal intent here
--     and apply it when the charge's anchor row is recorded (recordCreditedPaymentEvent).
-- =====================================================================

BEGIN;

CREATE OR REPLACE FUNCTION credit_reverse_grant(
  p_tenant UUID, p_user UUID, p_credits INTEGER, p_bucket TEXT,
  p_op_id TEXT, p_metadata JSONB DEFAULT '{}'::jsonb
) RETURNS TABLE(applied BOOLEAN, balance BIGINT, delta INTEGER)
LANGUAGE plpgsql AS $$
DECLARE v_ledger_id UUID; v_balance BIGINT; v_bal0 BIGINT; v_topup0 BIGINT; v_claw INTEGER;
BEGIN
  IF p_credits IS NULL OR p_credits <= 0 THEN RETURN QUERY SELECT false, 0::BIGINT, 0; RETURN; END IF;
  IF p_op_id IS NULL OR length(p_op_id) = 0 THEN RAISE EXCEPTION 'credit_reverse_grant requires an op_id'; END IF;

  SELECT COALESCE(balance,0), COALESCE(topup_balance,0) INTO v_bal0, v_topup0
    FROM credit_balances WHERE tenant_id = p_tenant;

  -- Actual clawable amount. TOPUP: the full pack (balance may go negative if spent).
  -- SUB: only the sub portion (balance − topup_balance) so the clamp can't eat the topup.
  IF p_bucket = 'topup' THEN
    v_claw := p_credits;
  ELSE
    v_claw := LEAST(p_credits, GREATEST(COALESCE(v_bal0,0) - COALESCE(v_topup0,0), 0))::INTEGER;
  END IF;

  IF v_claw <= 0 THEN
    -- Sub bucket already spent → nothing to claw. Record a zero-delta marker (idempotent)
    -- so a retry is a no-op; the money refund is still booked in payment_events/reversal_events.
    INSERT INTO credit_ledger (tenant_id, user_id, delta, reason, op_id, metadata)
    VALUES (p_tenant, p_user, 0, 'refund', p_op_id,
            COALESCE(p_metadata,'{}'::jsonb) || jsonb_build_object('bucket',COALESCE(p_bucket,'sub'),'note','nothing_to_claw'))
    ON CONFLICT (tenant_id, op_id) WHERE op_id IS NOT NULL DO NOTHING;
    RETURN QUERY SELECT true, COALESCE(v_bal0,0::BIGINT), 0; RETURN;
  END IF;

  INSERT INTO credit_ledger (tenant_id, user_id, delta, reason, op_id, metadata)
  VALUES (p_tenant, p_user, -v_claw, 'refund', p_op_id,
          COALESCE(p_metadata,'{}'::jsonb) || jsonb_build_object('bucket', COALESCE(p_bucket,'sub')))
  ON CONFLICT (tenant_id, op_id) WHERE op_id IS NOT NULL DO NOTHING
  RETURNING id INTO v_ledger_id;
  IF v_ledger_id IS NULL THEN                              -- replay of the SAME reversal event
    RETURN QUERY SELECT false, COALESCE(v_bal0, 0::BIGINT), 0; RETURN;
  END IF;

  IF p_bucket = 'topup' THEN
    UPDATE credit_balances cb SET
       topup_balance    = GREATEST(cb.topup_balance - LEAST(v_claw, cb.topup_balance), 0),
       topup_expires_at = CASE WHEN cb.topup_balance - LEAST(v_claw, cb.topup_balance) <= 0 THEN NULL ELSE cb.topup_expires_at END,
       balance          = cb.balance - v_claw,
       updated_at       = now()
     WHERE cb.tenant_id = p_tenant
     RETURNING cb.balance INTO v_balance;
  ELSE
    UPDATE credit_balances cb SET
       balance    = cb.balance - v_claw,            -- v_claw <= sub portion → balance stays >= topup_balance → clamp no-op
       updated_at = now()
     WHERE cb.tenant_id = p_tenant
     RETURNING cb.balance INTO v_balance;
  END IF;
  IF v_balance IS NULL THEN
    INSERT INTO credit_balances (tenant_id, balance) VALUES (p_tenant, -v_claw)
    ON CONFLICT (tenant_id) DO UPDATE SET balance = credit_balances.balance - v_claw, updated_at = now()
    RETURNING credit_balances.balance INTO v_balance;
  END IF;
  UPDATE credit_ledger SET balance_after = v_balance WHERE id = v_ledger_id;
  RETURN QUERY SELECT true, v_balance, -v_claw;
END; $$;

-- ── Out-of-order reversal queue (cross-tenant; the refund webhook has no tenant ctx) ──
CREATE TABLE IF NOT EXISTS orphan_reversals (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  provider            TEXT NOT NULL,
  provider_payment_id TEXT NOT NULL,
  refund_op_id        TEXT NOT NULL,
  kind                TEXT NOT NULL DEFAULT 'refund',
  refund_amount       BIGINT,
  raw_event           JSONB,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  applied_at          TIMESTAMPTZ,
  UNIQUE (provider, refund_op_id)
);
CREATE INDEX IF NOT EXISTS idx_orphan_reversals_pending
  ON orphan_reversals (provider, provider_payment_id) WHERE applied_at IS NULL;
GRANT SELECT, INSERT, UPDATE, DELETE ON orphan_reversals TO app_user;

COMMIT;
