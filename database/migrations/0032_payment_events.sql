-- =====================================================================
-- 0032_payment_events.sql
-- Shared payment-event ledger for the NEW payment rails (Dodo + Midtrans).
-- One row per payment event, the durable home of the shared grant core
-- (payments_core.grant_entitlement → credit_apply()).
--
-- DELIBERATELY NOT named `payments`: the unmerged feat/accounting-foundation
-- branch owns a heavy unified `payments(provider, external_ref…)` table. This
-- table stays independent so the new rails are mergeable WITHOUT that branch;
-- reconcile the two later (per the master spec storage rule). House style
-- already uses per-concern tables (processed_stripe_events), so this fits.
--
-- Both rails share ONE table via the `provider` column + a (provider,
-- idempotency_key) UNIQUE gate (Dodo idempotency_key = `webhook-id` header;
-- Midtrans idempotency_key = `order_id`). Stripe is untouched — it keeps its
-- own subscriptions/processed_stripe_events tables.
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS payment_events (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id             UUID        REFERENCES users(id) ON DELETE SET NULL,

    -- ── Provider / idempotency ────────────────────────────────────────────
    provider            TEXT        NOT NULL
                            CHECK (provider IN ('dodo','midtrans')),
    idempotency_key     TEXT        NOT NULL,            -- dodo: webhook-id ; midtrans: order_id
    provider_payment_id TEXT,                            -- dodo: data.payment_id ; midtrans: transaction_id

    -- ── Purchase details (server is authoritative on plan; amount = audit) ─
    plan_key            TEXT,                            -- 'starter' | 'pro' | 'studio'
    amount              BIGINT,                          -- dodo: minor units (cents) ; midtrans: IDR (whole rupiah)
    currency            TEXT,
    status              TEXT        NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','succeeded','failed','refunded',
                                              'disputed','cancelled','expired')),

    -- ── Exactly-once credit grant guard ───────────────────────────────────
    credited            BOOLEAN     NOT NULL DEFAULT FALSE,
    credits_granted     INTEGER,

    -- ── Audit ─────────────────────────────────────────────────────────────
    raw_event           JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- ── Timestamps ────────────────────────────────────────────────────────
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Exactly-once gate: one event row per (provider, idempotency_key).
    UNIQUE (provider, idempotency_key)
);

-- ── Indexes ──────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_payment_events_tenant   ON payment_events (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_payment_events_provider ON payment_events (provider, status);
CREATE INDEX IF NOT EXISTS idx_payment_events_ppid     ON payment_events (provider_payment_id);
-- Reconciliation helper: paid-but-not-yet-credited (mid-transaction crash / lost flip).
CREATE INDEX IF NOT EXISTS idx_payment_events_uncredited ON payment_events (provider, created_at)
                                                         WHERE credited = FALSE;

-- ── Comments ─────────────────────────────────────────────────────────────────
COMMENT ON TABLE  payment_events                 IS 'Shared payment-event ledger for the Dodo + Midtrans rails; the durable home of payments_core.grant_entitlement. Crediting itself goes through credit_apply()/credit_ledger. NOT the accounting branch payments table.';
COMMENT ON COLUMN payment_events.idempotency_key IS 'Dodo = Standard-Webhooks webhook-id header; Midtrans = order_id. UNIQUE per provider = exactly-once gate.';
COMMENT ON COLUMN payment_events.credited        IS 'TRUE once credits were granted in the SAME transaction that flipped this flag. Never grant outside that guard.';
COMMENT ON COLUMN payment_events.amount          IS 'Audit only. Dodo: minor units (cents). Midtrans: whole IDR. Interpret with currency.';

-- ── Row-Level Security (tenant isolation; FORCE so the owner cannot bypass) ──
ALTER TABLE payment_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE payment_events FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON payment_events;
CREATE POLICY tenant_isolation ON payment_events
    USING      (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

-- ── updated_at trigger (house convention) ──────────────────────────────────
DROP TRIGGER IF EXISTS trg_payment_events_updated_at ON payment_events;
CREATE TRIGGER trg_payment_events_updated_at
    BEFORE UPDATE ON payment_events
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── Grants (app_user is NOBYPASSRLS; explicit DML like the credit tables) ──
GRANT SELECT, INSERT, UPDATE, DELETE ON payment_events TO app_user;

-- ── Webhook tenant resolver (SECURITY DEFINER, same pattern as 0018/0024) ───────
-- The Midtrans /notification webhook is unauthenticated and its SIGNED payload
-- carries only order_id — not the tenant. payment_events is FORCE-RLS, so
-- app_user (NOBYPASSRLS) cannot read a row without already knowing the tenant
-- (chicken-and-egg). This runs as the table owner (BYPASSRLS) for this ONE
-- cross-tenant lookup, resolving the owning tenant/plan from the signed order_id
-- so the handler can set the tenant context and grant. Mirrors how the
-- unauthenticated /stream endpoint resolves a job's tenant (0018).
CREATE OR REPLACE FUNCTION payment_event_lookup(p_provider TEXT, p_idempotency_key TEXT)
RETURNS TABLE(tenant_id UUID, user_id UUID, plan_key TEXT, credited BOOLEAN, status TEXT)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public                 -- pinned: no caller-controlled search_path escalation
AS $$
  -- schema-qualified too (defense-in-depth on top of the pinned search_path)
  SELECT tenant_id, user_id, plan_key, credited, status
    FROM public.payment_events
   WHERE provider = p_provider AND idempotency_key = p_idempotency_key;
$$;
REVOKE ALL ON FUNCTION payment_event_lookup(TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION payment_event_lookup(TEXT, TEXT) TO app_user;

COMMIT;
