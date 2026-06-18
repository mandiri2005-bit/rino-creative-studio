-- 0030_credit_lifecycle.sql
-- ============================================================================
-- Credit lifecycle (Rino FINAL — ONE cap = 150 everywhere):
--   • Free leaky-bucket: +N/day (WIB), capped at a ceiling, NO expiry.
--   • Any grant (subscribe/renew/re-subscribe): balance = allowance + min(balance, cap).
--   • Lapse: grant_capped(allowance=0, cap) → keep ≤cap, drop to free leaky-bucket.
--
-- credit_apply() (0023) is additive-only and can't SET-to-target, so the cap +
-- ceiling clamps need two atomic SET primitives. Both: lock the balance row, write
-- ONE ledger row with the computed delta, SET balance to the target, idempotent on
-- (tenant, op_id). They RETURN the delta so the Redis cache can INCRBY it (which
-- preserves any outstanding holds: cache = durable − holds stays correct).
-- ============================================================================
BEGIN;

-- Allow the new ledger reasons --------------------------------------------------
ALTER TABLE credit_ledger DROP CONSTRAINT IF EXISTS credit_ledger_reason_check;
ALTER TABLE credit_ledger ADD  CONSTRAINT credit_ledger_reason_check
    CHECK (reason IN ('signup_grant','monthly_grant','topup','charge','refund','admin_adjust',
                      'daily_claim','period_grant','lapse'));

-- balance := p_allowance + LEAST(balance, p_carryover_cap) -----------------------
-- subscribe/renew/re-subscribe: allowance = plan credits, cap = 150.
-- lapse:                        allowance = 0,            cap = 150.
CREATE OR REPLACE FUNCTION credit_grant_capped(
    p_tenant UUID, p_user UUID, p_allowance INTEGER, p_carryover_cap INTEGER,
    p_reason TEXT, p_op_id TEXT DEFAULT NULL, p_metadata JSONB DEFAULT '{}'::jsonb
) RETURNS TABLE(applied BOOLEAN, balance BIGINT, delta INTEGER)
LANGUAGE plpgsql AS $$
DECLARE v_cur BIGINT; v_target BIGINT; v_delta INTEGER; v_ledger_id UUID; v_balance BIGINT;
BEGIN
    SELECT cb.balance INTO v_cur FROM credit_balances cb WHERE cb.tenant_id = p_tenant FOR UPDATE;
    v_cur := COALESCE(v_cur, 0);
    v_target := p_allowance + LEAST(v_cur, p_carryover_cap::BIGINT);
    v_delta  := (v_target - v_cur)::INTEGER;
    INSERT INTO credit_ledger (tenant_id, user_id, delta, reason, op_id, metadata)
    VALUES (p_tenant, p_user, v_delta, p_reason, p_op_id, COALESCE(p_metadata, '{}'::jsonb))
    ON CONFLICT (tenant_id, op_id) WHERE op_id IS NOT NULL DO NOTHING
    RETURNING id INTO v_ledger_id;
    IF v_ledger_id IS NOT NULL THEN
        INSERT INTO credit_balances (tenant_id, balance) VALUES (p_tenant, v_target)
        ON CONFLICT (tenant_id) DO UPDATE SET balance = EXCLUDED.balance, updated_at = now()
        RETURNING credit_balances.balance INTO v_balance;
        UPDATE credit_ledger SET balance_after = v_balance WHERE id = v_ledger_id;
        RETURN QUERY SELECT true, v_balance, v_delta;
    ELSE
        RETURN QUERY SELECT false, v_cur, 0;
    END IF;
END; $$;

-- balance := LEAST(p_ceiling, balance + p_daily)  (never reduces) ----------------
-- free daily leaky-bucket; op_id = daily_claim:{tenant}:{WIB-date} → one/day.
CREATE OR REPLACE FUNCTION credit_claim_daily(
    p_tenant UUID, p_user UUID, p_daily INTEGER, p_ceiling INTEGER,
    p_op_id TEXT, p_metadata JSONB DEFAULT '{}'::jsonb
) RETURNS TABLE(applied BOOLEAN, balance BIGINT, delta INTEGER)
LANGUAGE plpgsql AS $$
DECLARE v_cur BIGINT; v_target BIGINT; v_delta INTEGER; v_ledger_id UUID; v_balance BIGINT;
BEGIN
    SELECT cb.balance INTO v_cur FROM credit_balances cb WHERE cb.tenant_id = p_tenant FOR UPDATE;
    v_cur := COALESCE(v_cur, 0);
    v_target := LEAST(p_ceiling::BIGINT, v_cur + p_daily);
    v_delta  := (v_target - v_cur)::INTEGER;
    IF v_delta < 0 THEN v_delta := 0; v_target := v_cur; END IF;   -- claim never reduces
    INSERT INTO credit_ledger (tenant_id, user_id, delta, reason, op_id, metadata)
    VALUES (p_tenant, p_user, v_delta, 'daily_claim', p_op_id, COALESCE(p_metadata, '{}'::jsonb))
    ON CONFLICT (tenant_id, op_id) WHERE op_id IS NOT NULL DO NOTHING
    RETURNING id INTO v_ledger_id;
    IF v_ledger_id IS NOT NULL THEN
        INSERT INTO credit_balances (tenant_id, balance) VALUES (p_tenant, v_target)
        ON CONFLICT (tenant_id) DO UPDATE SET balance = EXCLUDED.balance, updated_at = now()
        RETURNING credit_balances.balance INTO v_balance;
        UPDATE credit_ledger SET balance_after = v_balance WHERE id = v_ledger_id;
        RETURN QUERY SELECT true, v_balance, v_delta;
    ELSE
        RETURN QUERY SELECT false, v_cur, 0;
    END IF;
END; $$;

GRANT EXECUTE ON FUNCTION credit_grant_capped(UUID,UUID,INTEGER,INTEGER,TEXT,TEXT,JSONB) TO app_user;
GRANT EXECUTE ON FUNCTION credit_claim_daily(UUID,UUID,INTEGER,INTEGER,TEXT,JSONB)        TO app_user;

COMMIT;
