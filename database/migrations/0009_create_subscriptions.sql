-- =====================================================================
-- 0009_create_subscriptions.sql
-- Stripe subscription state mirrored per tenant
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS subscriptions (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- ── Stripe identifiers ────────────────────────────────────────────────
    stripe_customer_id      TEXT        NOT NULL,
    stripe_subscription_id  TEXT        NOT NULL UNIQUE,
    stripe_price_id         TEXT        NOT NULL,
    stripe_product_id       TEXT        NOT NULL,

    -- ── Plan / status ─────────────────────────────────────────────────────
    plan                    TEXT        NOT NULL
                                CHECK (plan IN ('free', 'starter', 'pro', 'enterprise')),
    status                  TEXT        NOT NULL
                                CHECK (status IN (
                                    'trialing', 'active', 'past_due', 'unpaid',
                                    'cancelled', 'incomplete', 'incomplete_expired',
                                    'paused'
                                )),

    -- ── Billing cycle ─────────────────────────────────────────────────────
    current_period_start    TIMESTAMPTZ NOT NULL,
    current_period_end      TIMESTAMPTZ NOT NULL,
    trial_start             TIMESTAMPTZ,
    trial_end               TIMESTAMPTZ,
    cancel_at               TIMESTAMPTZ,
    cancelled_at            TIMESTAMPTZ,
    ended_at                TIMESTAMPTZ,

    -- ── Usage-based limits ────────────────────────────────────────────────
    monthly_token_limit     BIGINT,
    monthly_job_limit       INTEGER,
    seats                   SMALLINT    NOT NULL DEFAULT 1,

    -- ── Timestamps ───────────────────────────────────────────────────────
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Indexes ─────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_subscriptions_tenant_id              ON subscriptions (tenant_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_stripe_customer_id     ON subscriptions (stripe_customer_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_stripe_subscription_id ON subscriptions (stripe_subscription_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_status                 ON subscriptions (status);
CREATE INDEX IF NOT EXISTS idx_subscriptions_current_period_end     ON subscriptions (current_period_end)
                                                                    WHERE status IN ('active', 'trialing');

-- ── Comments ────────────────────────────────────────────────────────────────
COMMENT ON TABLE  subscriptions                      IS 'Mirrors Stripe subscription state per tenant. One active row at a time; history is retained.';
COMMENT ON COLUMN subscriptions.stripe_subscription_id IS 'Stripe sub_... identifier; used for webhook reconciliation.';
COMMENT ON COLUMN subscriptions.monthly_token_limit  IS 'Denormalised from Stripe metadata. NULL = plan has no token cap.';

-- ── Row-Level Security ──────────────────────────────────────────────────────
ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON subscriptions;
CREATE POLICY tenant_isolation ON subscriptions
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

-- ── updated_at trigger ──────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_subscriptions_updated_at ON subscriptions;
CREATE TRIGGER trg_subscriptions_updated_at
    BEFORE UPDATE ON subscriptions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMIT;
