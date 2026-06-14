-- =====================================================================
-- 0023_credit_ledger.sql
-- Step 4 — the durable money layer behind the pre-funded credit gate.
--
--   credit_balances  : tenant_id → current balance (O(1) durable truth).
--   credit_ledger    : append-only audit of every +/- (grant/topup/charge/refund).
--   usage_logs.credits : credits charged per AI call (the per-op ledger line).
--   processed_stripe_events : webhook idempotency (events double-deliver).
--   credit_apply()   : atomic "append ledger + move balance", idempotent on op_id.
--
-- Redis (bal:{tenant}:credits) is only a hot CACHE in front of credit_balances;
-- this table is the source of truth and what reconcile() checks against.
-- =====================================================================

BEGIN;

-- ── 1. Per-op credit charge recorded alongside the USD cost ───────────────────
ALTER TABLE usage_logs
    ADD COLUMN IF NOT EXISTS credits INTEGER NOT NULL DEFAULT 0;
COMMENT ON COLUMN usage_logs.credits IS 'Credits charged for this call (Step 4 metering). 0 for BYOK / free ops.';

-- ── 2. Durable balance: one row per tenant ────────────────────────────────────
CREATE TABLE IF NOT EXISTS credit_balances (
    tenant_id   UUID        PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    balance     BIGINT      NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE credit_balances IS 'Durable source of truth for credit balance; Redis fronts it as a cache.';

-- ── 3. Append-only ledger: every credit movement, with idempotency key ────────
CREATE TABLE IF NOT EXISTS credit_ledger (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id     UUID        REFERENCES users(id) ON DELETE SET NULL,
    delta       INTEGER     NOT NULL,                     -- + grant/topup/refund, - charge
    reason      TEXT        NOT NULL
                    CHECK (reason IN ('signup_grant','monthly_grant','topup',
                                      'charge','refund','admin_adjust')),
    op_id       TEXT,                                     -- idempotency key (stripe evt, job id…)
    balance_after BIGINT,                                 -- snapshot for audit
    metadata    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE credit_ledger IS 'Append-only audit of credit movements. op_id makes grants/topups idempotent.';

CREATE INDEX IF NOT EXISTS idx_credit_ledger_tenant   ON credit_ledger (tenant_id, created_at DESC);
-- one ledger row per (tenant, op_id): the heart of idempotent top-ups/grants
CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_ledger_tenant_op
    ON credit_ledger (tenant_id, op_id) WHERE op_id IS NOT NULL;

-- ── 4. Stripe webhook idempotency (global; webhooks resolve tenant later) ──────
CREATE TABLE IF NOT EXISTS processed_stripe_events (
    stripe_event_id TEXT        PRIMARY KEY,
    event_type      TEXT,
    processed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE processed_stripe_events IS 'Records handled Stripe event ids so double-delivered webhooks are ignored.';

-- ── 5. Atomic "append ledger + move balance" — idempotent on op_id ────────────
-- SECURITY INVOKER: runs as the calling role (app_user) under its RLS context,
-- so the caller MUST have set app.current_tenant_id = p_tenant. Returns whether
-- the movement was newly applied (false = duplicate op_id) and the new balance.
CREATE OR REPLACE FUNCTION credit_apply(
    p_tenant   UUID,
    p_user     UUID,
    p_delta    INTEGER,
    p_reason   TEXT,
    p_op_id    TEXT          DEFAULT NULL,
    p_metadata JSONB         DEFAULT '{}'::jsonb
) RETURNS TABLE(applied BOOLEAN, balance BIGINT)
LANGUAGE plpgsql
AS $$
DECLARE
    v_ledger_id UUID;
    v_balance   BIGINT;
BEGIN
    IF p_op_id IS NOT NULL THEN
        INSERT INTO credit_ledger (tenant_id, user_id, delta, reason, op_id, metadata)
        VALUES (p_tenant, p_user, p_delta, p_reason, p_op_id, COALESCE(p_metadata, '{}'::jsonb))
        ON CONFLICT (tenant_id, op_id) WHERE op_id IS NOT NULL DO NOTHING
        RETURNING id INTO v_ledger_id;        -- NULL on conflict (already applied)
    ELSE
        INSERT INTO credit_ledger (tenant_id, user_id, delta, reason, op_id, metadata)
        VALUES (p_tenant, p_user, p_delta, p_reason, NULL, COALESCE(p_metadata, '{}'::jsonb))
        RETURNING id INTO v_ledger_id;
    END IF;

    IF v_ledger_id IS NOT NULL THEN
        INSERT INTO credit_balances (tenant_id, balance)
        VALUES (p_tenant, p_delta)
        ON CONFLICT (tenant_id)
            DO UPDATE SET balance = credit_balances.balance + EXCLUDED.balance,
                          updated_at = now()
        RETURNING credit_balances.balance INTO v_balance;
        UPDATE credit_ledger SET balance_after = v_balance WHERE id = v_ledger_id;
        RETURN QUERY SELECT true, v_balance;
    ELSE
        SELECT cb.balance INTO v_balance FROM credit_balances cb WHERE cb.tenant_id = p_tenant;
        RETURN QUERY SELECT false, COALESCE(v_balance, 0::BIGINT);
    END IF;
END;
$$;

-- ── 6. Row-Level Security (mirror the subscriptions pattern) ──────────────────
ALTER TABLE credit_balances ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON credit_balances;
CREATE POLICY tenant_isolation ON credit_balances
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

ALTER TABLE credit_ledger ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON credit_ledger;
CREATE POLICY tenant_isolation ON credit_ledger
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);
-- processed_stripe_events is intentionally NOT RLS-protected: it holds no tenant
-- data and is written by the webhook before a tenant context exists.

-- ── 7. updated_at trigger on balances ─────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_credit_balances_updated_at ON credit_balances;
CREATE TRIGGER trg_credit_balances_updated_at
    BEFORE UPDATE ON credit_balances
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── 8. Grants for the app role (explicit; default privileges may predate it) ──
GRANT SELECT, INSERT, UPDATE, DELETE ON credit_balances        TO app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON credit_ledger          TO app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON processed_stripe_events TO app_user;
GRANT EXECUTE ON FUNCTION credit_apply(UUID, UUID, INTEGER, TEXT, TEXT, JSONB) TO app_user;

COMMIT;
