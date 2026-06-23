-- =====================================================================
-- 0032_ledger_hardening.sql
-- Hardens the credit ledger the accounting posting engine reads from.
-- These are the audit-confirmed PREREQUISITES for a sound posting engine:
-- the derive engine assumes a non-negative, tenant-isolated, append-correct
-- ledger. Additive + idempotent; no behavioural change for correct callers.
--
--   #37 (HIGH)  credit_apply()/credit_grant_capped() are additive UPDATEs with
--               NO balance floor -> a TOCTOU race past the gate drove the durable
--               balance to -547 in prod. Add CHECK(balance >= 0) as the durable
--               backstop (NOT VALID so it applies even if a negative row exists
--               today; it still rejects every NEW write that would go negative),
--               plus an atomic guarded-debit primitive the hot path/engine can
--               adopt to fail gracefully (applied=false) instead of raising.
--   #39 (LOW)   credit_balances / credit_ledger RLS shipped USING-only (0023/0025)
--               -> a tenant could INSERT/UPDATE a row carrying ANOTHER tenant_id.
--               Add WITH CHECK to both policies (write-side isolation).
--   E5 support  credit_ledger.reason CHECK gains 'breakage' so lot-expiry events
--               (PSAK 72 B44-B47) have a durable ledger reason to post from.
--
-- NOTE (deferred to Phase 2/3, NOT here): credit_lots is SOURCE-written at
-- grant/topup time (the grant fns write a lot with the GROSS allowance + is_paid
-- + price + payment link), NOT derived from the NET (target-current) ledger
-- delta. That wiring rides with the cash-capture work because a PAID lot must be
-- born with its payment row; doing it in isolation here would mis-size lots
-- against the carry-over cap. This migration only hardens the existing ledger.
-- =====================================================================

BEGIN;

-- 1. E5 breakage reason ------------------------------------------------------------
ALTER TABLE credit_ledger DROP CONSTRAINT IF EXISTS credit_ledger_reason_check;
ALTER TABLE credit_ledger ADD  CONSTRAINT credit_ledger_reason_check
    CHECK (reason IN ('signup_grant','monthly_grant','topup','charge','refund',
                      'admin_adjust','daily_claim','period_grant','lapse',
                      'breakage'));

-- 2. #37 durable balance floor -----------------------------------------------------
-- NOT VALID: do not fail the migration if a legacy negative row exists; the
-- constraint still rejects every subsequent INSERT/UPDATE that would go negative.
-- Run `ALTER TABLE credit_balances VALIDATE CONSTRAINT credit_balances_nonneg;`
-- once any historical negatives are reconciled to fully enforce retroactively.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'credit_balances_nonneg') THEN
        ALTER TABLE credit_balances
            ADD CONSTRAINT credit_balances_nonneg CHECK (balance >= 0) NOT VALID;
    END IF;
END $$;

-- Atomic guarded debit: decrement only if affordable, in ONE statement (no
-- read-then-write TOCTOU). Returns applied=false + the unchanged balance when the
-- tenant can't cover p_amount, so callers fail gracefully instead of overdrawing
-- or catching a CHECK exception. Idempotent on op_id via the credit_ledger unique
-- index. SECURITY INVOKER -> runs under the caller's RLS (set app.current_tenant_id).
CREATE OR REPLACE FUNCTION credit_debit_guarded(
    p_tenant   UUID,
    p_user     UUID,
    p_amount   INTEGER,                       -- positive credits to charge
    p_reason   TEXT          DEFAULT 'charge',
    p_op_id    TEXT          DEFAULT NULL,
    p_metadata JSONB         DEFAULT '{}'::jsonb
) RETURNS TABLE(applied BOOLEAN, balance BIGINT)
LANGUAGE plpgsql AS $$
DECLARE
    v_ledger_id UUID;
    v_balance   BIGINT;
BEGIN
    IF p_amount <= 0 THEN
        SELECT cb.balance INTO v_balance FROM credit_balances cb WHERE cb.tenant_id = p_tenant;
        RETURN QUERY SELECT false, COALESCE(v_balance, 0::BIGINT);
        RETURN;
    END IF;
    -- idempotency: a replayed op_id never double-charges.
    IF p_op_id IS NOT NULL THEN
        INSERT INTO credit_ledger (tenant_id, user_id, delta, reason, op_id, metadata)
        VALUES (p_tenant, p_user, -p_amount, p_reason, p_op_id, COALESCE(p_metadata,'{}'::jsonb))
        ON CONFLICT (tenant_id, op_id) WHERE op_id IS NOT NULL DO NOTHING
        RETURNING id INTO v_ledger_id;
        IF v_ledger_id IS NULL THEN          -- duplicate op_id: already charged
            SELECT cb.balance INTO v_balance FROM credit_balances cb WHERE cb.tenant_id = p_tenant;
            RETURN QUERY SELECT false, COALESCE(v_balance, 0::BIGINT);
            RETURN;
        END IF;
    ELSE
        INSERT INTO credit_ledger (tenant_id, user_id, delta, reason, op_id, metadata)
        VALUES (p_tenant, p_user, -p_amount, p_reason, NULL, COALESCE(p_metadata,'{}'::jsonb))
        RETURNING id INTO v_ledger_id;
    END IF;
    -- atomic check-and-decrement: only succeeds if the row can cover it.
    -- Columns are table-qualified: the OUT param `balance` would otherwise shadow
    -- the credit_balances.balance column in WHERE/SET/RETURNING (ambiguous-ref).
    UPDATE credit_balances
       SET balance = credit_balances.balance - p_amount, updated_at = now()
     WHERE credit_balances.tenant_id = p_tenant
       AND credit_balances.balance  >= p_amount
     RETURNING credit_balances.balance INTO v_balance;
    IF NOT FOUND THEN                        -- insufficient funds -> reverse the ledger row
        DELETE FROM credit_ledger WHERE id = v_ledger_id;
        SELECT cb.balance INTO v_balance FROM credit_balances cb WHERE cb.tenant_id = p_tenant;
        RETURN QUERY SELECT false, COALESCE(v_balance, 0::BIGINT);
        RETURN;
    END IF;
    UPDATE credit_ledger SET balance_after = v_balance WHERE id = v_ledger_id;
    RETURN QUERY SELECT true, v_balance;
END;
$$;

GRANT EXECUTE ON FUNCTION credit_debit_guarded(UUID,UUID,INTEGER,TEXT,TEXT,JSONB) TO app_user;

-- 3. #39 write-side RLS isolation (add WITH CHECK to the USING-only policies) -------
DROP POLICY IF EXISTS tenant_isolation ON credit_balances;
CREATE POLICY tenant_isolation ON credit_balances
    USING      (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

DROP POLICY IF EXISTS tenant_isolation ON credit_ledger;
CREATE POLICY tenant_isolation ON credit_ledger
    USING      (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

COMMIT;
