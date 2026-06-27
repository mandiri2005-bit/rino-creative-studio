-- =====================================================================
-- 0047_reverse_grant_for_update.sql
-- Audit MEDIUM: credit_reverse_grant read the balance WITHOUT FOR UPDATE
-- (0045 line 22), while every other credit function locks the row. Two
-- concurrent refund/chargeback webhooks for the same tenant could both read
-- the same stale (v_bal0, v_topup0) and both compute a claw against it →
-- over-claw / topup destruction under a TOCTOU race. Add FOR UPDATE so the
-- balance read serialises with the UPDATE below (and with reset/topup/sweep,
-- which already lock). Behaviour otherwise IDENTICAL to 0045.
-- Re-GRANTs to app_user (belt-and-suspenders; the fn had no explicit grant
-- before 0046).
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

  -- FOR UPDATE: lock the balance row so concurrent reversals serialise (was missing).
  SELECT COALESCE(cb.balance,0), COALESCE(cb.topup_balance,0) INTO v_bal0, v_topup0
    FROM credit_balances cb WHERE cb.tenant_id = p_tenant FOR UPDATE;

  IF p_bucket = 'topup' THEN
    v_claw := p_credits;
  ELSE
    v_claw := LEAST(p_credits, GREATEST(COALESCE(v_bal0,0) - COALESCE(v_topup0,0), 0))::INTEGER;
  END IF;

  IF v_claw <= 0 THEN
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
  IF v_ledger_id IS NULL THEN
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
       balance    = cb.balance - v_claw,
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

GRANT EXECUTE ON FUNCTION credit_reverse_grant(UUID,UUID,INTEGER,TEXT,TEXT,JSONB) TO app_user;

COMMIT;
