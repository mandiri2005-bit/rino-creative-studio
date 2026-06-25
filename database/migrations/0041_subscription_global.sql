-- =====================================================================
-- 0041_subscription_global.sql
-- GLOBAL deployment subscription billing (Dodo, recurring monthly).
--
-- ADDITIVE + idempotent. Active ONLY in the global deployment at runtime
-- (gated by BILLING_MODE=subscription in app code). On the Indonesia
-- (BILLING_MODE=one_time) deployment these tables/functions simply exist
-- unused — they never run because no subscription is ever created there.
-- The one-time path (grant_entitlement / Midtrans / credit_apply) is UNTOUCHED.
--
-- Two new tables + helpers:
--   1a. dodo_subscriptions  — one row per Dodo subscription (NOT the Stripe
--       `subscriptions` table from 0009; provider-scoped, collision-free).
--   1b. free_grants         — once-per-email lifetime guard (GLOBAL, cross-tenant;
--       NOT tenant-RLS — the whole point is to block the same email across accounts).
--
-- RESET semantics (use-it-or-lose-it) REUSE the existing `credit_grant_capped`
-- (0030) with carryover_cap=0 → balance = allowance + LEAST(balance,0) = allowance.
-- No new credit primitive is added; reset_entitlement() (payments_core.mjs) calls
-- credit_grant_capped(cap=0). This keeps ONE battle-tested set-to-target SQL fn.
--
-- Also extends payment_events.provider CHECK to allow 'dodo_sub' so recurring
-- charges can be recorded in the shared event ledger for idempotency/audit.
-- =====================================================================

BEGIN;

-- ── 0. payment_events: allow the new 'dodo_sub' provider ──────────────────────
-- The shared event ledger (0032) gates one-time rails ('dodo','midtrans'). The
-- subscription webhook records recurring period grants under 'dodo_sub' so the
-- (provider, idempotency_key=webhook-id) UNIQUE remains the exactly-once gate.
ALTER TABLE payment_events DROP CONSTRAINT IF EXISTS payment_events_provider_check;
ALTER TABLE payment_events ADD  CONSTRAINT payment_events_provider_check
    CHECK (provider IN ('dodo','midtrans','dodo_sub'));

-- =====================================================================
-- 1a. dodo_subscriptions
-- =====================================================================
CREATE TABLE IF NOT EXISTS dodo_subscriptions (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id               UUID        REFERENCES users(id) ON DELETE SET NULL,

    plan_key              TEXT        NOT NULL,                  -- 'starter'|'plus'|'pro'|'ultra' (global config)
    dodo_subscription_id  TEXT        NOT NULL UNIQUE,           -- Dodo sub_… (UNIQUE = one row per Dodo sub)
    dodo_customer_id      TEXT,                                  -- Dodo cus_… (for the customer portal)

    -- Dodo's ACTUAL status set (verified vs docs): NO 'past_due' — failed renewal
    -- sits in 'on_hold' during dunning; 'failed' is only initial mandate failure.
    status                TEXT        NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending','active','on_hold','cancelled','expired','failed')),
    cadence               TEXT        NOT NULL DEFAULT 'monthly',

    -- Period bounds mapped from Dodo's field names (previous_billing_date →
    -- current_period_start ; next_billing_date → current_period_end). Dodo does
    -- NOT use current_period_*; we normalise to these column names internally.
    current_period_start  TIMESTAMPTZ,
    current_period_end    TIMESTAMPTZ,
    -- Mapped from Dodo's cancel_at_next_billing_date (NOT cancel_at_period_end).
    cancel_at_period_end  BOOLEAN     NOT NULL DEFAULT FALSE,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dodo_subs_tenant_user ON dodo_subscriptions (tenant_id, user_id);
-- Cron reconcile fallback (Dodo may leave a dunning-exhausted sub in 'on_hold'
-- forever with no terminal event): scan non-terminal subs past their period end.
CREATE INDEX IF NOT EXISTS idx_dodo_subs_reconcile   ON dodo_subscriptions (status, current_period_end)
    WHERE status IN ('on_hold','cancelled');

COMMENT ON TABLE  dodo_subscriptions IS 'Global-deployment Dodo recurring subscriptions. Separate from Stripe subscriptions (0009). Credits use RESET semantics (credit_grant_capped cap=0), not ADD.';
COMMENT ON COLUMN dodo_subscriptions.status IS 'Dodo status set: pending|active|on_hold|cancelled|expired|failed. on_hold = renewal failing (dunning); NO past_due (Stripe term).';
COMMENT ON COLUMN dodo_subscriptions.current_period_end IS 'Mapped from Dodo next_billing_date. Used as the period key for once-per-period credit reset and the cron downgrade fallback.';

-- RLS (tenant isolation; FORCE so the owner cannot bypass, same as payment_events).
ALTER TABLE dodo_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE dodo_subscriptions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON dodo_subscriptions;
CREATE POLICY tenant_isolation ON dodo_subscriptions
    USING      (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

DROP TRIGGER IF EXISTS trg_dodo_subs_updated_at ON dodo_subscriptions;
CREATE TRIGGER trg_dodo_subs_updated_at
    BEFORE UPDATE ON dodo_subscriptions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

GRANT SELECT, INSERT, UPDATE, DELETE ON dodo_subscriptions TO app_user;

-- Webhook tenant resolver (SECURITY DEFINER; same pattern as payment_event_lookup
-- in 0032). A subscription webhook is unauthenticated; we prefer the tenant_id from
-- the event metadata, but fall back to this owner-run lookup keyed on the unguessable
-- dodo_subscription_id when metadata is absent (e.g. some renewal payloads).
CREATE OR REPLACE FUNCTION dodo_subscription_lookup(p_dodo_subscription_id TEXT)
RETURNS TABLE(tenant_id UUID, user_id UUID, plan_key TEXT, status TEXT)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT tenant_id, user_id, plan_key, status
    FROM public.dodo_subscriptions
   WHERE dodo_subscription_id = p_dodo_subscription_id;
$$;
REVOKE ALL ON FUNCTION dodo_subscription_lookup(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION dodo_subscription_lookup(TEXT) TO app_user;

-- Cron reconcile scan (SECURITY DEFINER): the dunning-exhaustion fallback needs to
-- read CROSS-TENANT (subs stuck on_hold/cancelled past their period end), but the
-- table is FORCE-RLS so app_user (NOBYPASSRLS) cannot. This owner-run scan returns
-- only the minimal fields the cron needs to verify-then-downgrade each sub. It does
-- not move credits — the cron re-enters per-tenant (RLS-scoped) to do that.
CREATE OR REPLACE FUNCTION dodo_subscriptions_due_for_reconcile(p_cutoff_epoch BIGINT, p_limit INT)
RETURNS TABLE(tenant_id UUID, user_id UUID, dodo_subscription_id TEXT, current_period_end TIMESTAMPTZ)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT tenant_id, user_id, dodo_subscription_id, current_period_end
    FROM public.dodo_subscriptions
   WHERE status IN ('on_hold','cancelled')
     AND current_period_end IS NOT NULL
     AND current_period_end < to_timestamp(p_cutoff_epoch)
   ORDER BY current_period_end ASC
   LIMIT p_limit;
$$;
REVOKE ALL ON FUNCTION dodo_subscriptions_due_for_reconcile(BIGINT, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION dodo_subscriptions_due_for_reconcile(BIGINT, INT) TO app_user;

-- =====================================================================
-- 1b. free_grants — once-per-email lifetime guard (GLOBAL, NOT tenant-scoped)
-- =====================================================================
-- Free is 500 credits ONCE per verified email, ever — across ALL accounts/tenants.
-- Therefore this guard is intentionally GLOBAL (not RLS-tenant-scoped): an attacker
-- spinning up a second tenant with the same email must still be blocked. The email
-- is normalised by the caller (lowercase; gmail dots/+tags stripped) before hashing.
CREATE TABLE IF NOT EXISTS free_grants (
    email_norm   TEXT        PRIMARY KEY,                  -- normalised+hashed email (cross-account unique)
    user_id      UUID,
    tenant_id    UUID,
    raw_domain   TEXT,                                     -- for disposable-domain auditing
    granted_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE free_grants IS 'Lifetime once-per-email Free guard (GLOBAL, cross-tenant). Email normalised (gmail dots/+tags stripped) then hashed by the caller. Written ONLY by the SECURITY DEFINER claim_free_grant().';

-- Locked down: 0016 ALTER DEFAULT PRIVILEGES auto-grants app_user DML on every new
-- table. For this guard that would let a tenant DELETE its own row to re-farm free
-- credits, or enumerate every signup email. All access goes through claim_free_grant
-- (SECURITY DEFINER, owner). Mirrors the free_claims lockdown pattern.
REVOKE ALL ON free_grants FROM app_user;

-- =====================================================================
-- 1c. RESET semantics — REUSE credit_grant_capped (0030), no new fn
-- =====================================================================
-- The global product's use-it-or-lose-it reset is NOT a new primitive: 0030 already
-- ships credit_grant_capped(p_tenant,p_user,p_allowance,p_carryover_cap,p_reason,
-- p_op_id,p_metadata) → balance = p_allowance + LEAST(balance, p_carryover_cap),
-- idempotent on (tenant_id, op_id), returning (applied, balance, delta). Calling it
-- with carryover_cap=0 gives balance = allowance (pure reset; any unspent forfeited).
-- reset_entitlement() (payments_core.mjs) calls it with cap=0. Reasons stay within
-- the 0033 CHECK set ('monthly_grant' for active/renewed/plan_changed, 'lapse' for
-- expiry). Nothing to define here — this keeps ONE tested set-to-target SQL fn.

-- =====================================================================
-- 1b (cont). claim_free_grant() — atomic once-per-email Free grant (SECURITY DEFINER)
-- =====================================================================
-- Owner-run (BYPASSRLS on the global free_grants guard). Inserts the lifetime guard
-- row; if newly inserted, grants the initial Free credits via credit_apply (ADD,
-- one-time — op_id keyed on the email so a retry is idempotent). If the email was
-- already claimed (anywhere, any tenant) → NO grant. Free has NO monthly refresh.
CREATE OR REPLACE FUNCTION claim_free_grant(
    p_email_norm TEXT, p_user UUID, p_tenant UUID, p_domain TEXT, p_credits INTEGER
) RETURNS TABLE(status TEXT, balance BIGINT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
#variable_conflict use_column
DECLARE v_prev TEXT; v_bal BIGINT;
BEGIN
    v_prev := current_setting('app.current_tenant_id', true);   -- restore on exit (DEFINER GUC hygiene)

    INSERT INTO free_grants (email_norm, user_id, tenant_id, raw_domain)
    VALUES (p_email_norm, p_user, p_tenant, p_domain)
    ON CONFLICT (email_norm) DO NOTHING;
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'already_claimed'::TEXT, 0::BIGINT; RETURN;
    END IF;

    -- First-ever claim for this email → grant the one-time Free credits (ADD).
    PERFORM set_config('app.current_tenant_id', p_tenant::text, true);
    SELECT balance INTO v_bal
      FROM credit_apply(p_tenant, p_user, p_credits, 'signup_grant',
                        'free_grant:'||p_email_norm,
                        jsonb_build_object('tier','free','source','signup'));
    PERFORM set_config('app.current_tenant_id', COALESCE(v_prev,''), true);
    RETURN QUERY SELECT 'granted'::TEXT, COALESCE(v_bal,0::BIGINT);
END; $$;
REVOKE ALL ON FUNCTION claim_free_grant(TEXT,UUID,UUID,TEXT,INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_free_grant(TEXT,UUID,UUID,TEXT,INTEGER) TO app_user;

-- =====================================================================
-- 1d. Top-up two-bucket (Opsi A) — sub_balance + topup_balance on credit_balances
-- =====================================================================
-- `balance` STAYS the authoritative TOTAL (Redis cache, the hold/charge gate, the
-- Indonesia one-time path — ALL unchanged). We add a topup BUCKET alongside it:
--   topup_balance     — credits bought as one-time top-ups (ADD, own expiry)
--   topup_expires_at   — uniform expiry (= the renewal AFTER the imminent one;
--                        Free = +30d). One date suffices (GREATEST on re-topup).
--   sub_balance        — GENERATED = balance - topup_balance (the subscription
--                        portion). Derived → cannot drift, zero maintenance.
-- Consume order (sub BEFORE topup) is enforced WITHOUT touching charge()/hold():
-- a trigger clamps topup_balance := LEAST(topup_balance, balance) on every write,
-- so topup only shrinks once balance drops to/below it (i.e. the sub portion is
-- gone). Indonesia keeps topup_balance=0 → the clamp is a no-op → identical.
ALTER TABLE credit_balances
    ADD COLUMN IF NOT EXISTS topup_balance    BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS topup_expires_at TIMESTAMPTZ;
-- sub_balance is generated; add only if missing (guard re-run).
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_name='credit_balances' AND column_name='sub_balance') THEN
    ALTER TABLE credit_balances
      ADD COLUMN sub_balance BIGINT GENERATED ALWAYS AS (balance - topup_balance) STORED;
  END IF;
END $$;
COMMENT ON COLUMN credit_balances.topup_balance    IS 'One-time top-up credits (ADD, expiry=topup_expires_at). Part of balance; consumed AFTER sub_balance via the clamp trigger.';
COMMENT ON COLUMN credit_balances.topup_expires_at IS 'Uniform top-up expiry = renewal after the imminent one (min ~30d); Free = +30d. GREATEST on re-topup.';
COMMENT ON COLUMN credit_balances.sub_balance      IS 'Generated: balance - topup_balance. The subscription (RESET-able) portion. The clamp trigger guarantees topup_balance <= balance (so sub_balance >= 0) ONLY while balance >= 0; a refund-into-negative balance makes sub_balance negative too (intentional — do NOT add CHECK(sub_balance>=0), it would abort refunds). The display clamps it to >= 0.';

-- Clamp trigger: topup_balance can never exceed the total or go negative. This is
-- what makes "consume sub first" automatic and keeps sub_balance (generated) >= 0.
CREATE OR REPLACE FUNCTION clamp_topup_balance() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.topup_balance := LEAST(GREATEST(NEW.topup_balance, 0), GREATEST(NEW.balance, 0));
    RETURN NEW;
END; $$;
DROP TRIGGER IF EXISTS trg_credit_balances_topup_clamp ON credit_balances;
CREATE TRIGGER trg_credit_balances_topup_clamp
    BEFORE INSERT OR UPDATE ON credit_balances
    FOR EACH ROW EXECUTE FUNCTION clamp_topup_balance();

-- credit_topup_grant: ADD a one-time top-up. Idempotent on (tenant_id, op_id).
-- Grows balance + topup_balance together; sets topup_expires_at = furthest of the
-- existing and the new (extend, never shorten). reason='topup' (0033 CHECK set).
-- SECURITY INVOKER (caller sets app.current_tenant_id, like credit_grant_capped).
CREATE OR REPLACE FUNCTION credit_topup_grant(
    p_tenant UUID, p_user UUID, p_amount INTEGER, p_op_id TEXT,
    p_expires_at TIMESTAMPTZ, p_metadata JSONB DEFAULT '{}'::jsonb
) RETURNS TABLE(applied BOOLEAN, balance BIGINT, delta INTEGER)
LANGUAGE plpgsql AS $$
DECLARE v_ledger_id UUID; v_balance BIGINT;
BEGIN
    IF p_amount IS NULL OR p_amount <= 0 THEN RAISE EXCEPTION 'credit_topup_grant amount must be > 0 (got %)', p_amount; END IF;
    IF p_op_id IS NULL OR length(p_op_id) = 0 THEN RAISE EXCEPTION 'credit_topup_grant requires an op_id'; END IF;
    INSERT INTO credit_ledger (tenant_id, user_id, delta, reason, op_id, metadata)
    VALUES (p_tenant, p_user, p_amount, 'topup', p_op_id,
            COALESCE(p_metadata,'{}'::jsonb) || jsonb_build_object('bucket','topup','expires_at', p_expires_at))
    ON CONFLICT (tenant_id, op_id) WHERE op_id IS NOT NULL DO NOTHING
    RETURNING id INTO v_ledger_id;
    IF v_ledger_id IS NULL THEN
        SELECT cb.balance INTO v_balance FROM credit_balances cb WHERE cb.tenant_id = p_tenant;
        RETURN QUERY SELECT false, COALESCE(v_balance, 0::BIGINT), 0; RETURN;
    END IF;
    INSERT INTO credit_balances (tenant_id, balance, topup_balance, topup_expires_at)
    VALUES (p_tenant, p_amount, p_amount, p_expires_at)
    ON CONFLICT (tenant_id) DO UPDATE SET
        balance          = credit_balances.balance + EXCLUDED.balance,
        topup_balance    = credit_balances.topup_balance + EXCLUDED.topup_balance,
        topup_expires_at = GREATEST(credit_balances.topup_expires_at, EXCLUDED.topup_expires_at),  -- furthest; NULLs ignored
        updated_at       = now()
    RETURNING credit_balances.balance INTO v_balance;
    UPDATE credit_ledger SET balance_after = v_balance WHERE id = v_ledger_id;
    RETURN QUERY SELECT true, v_balance, p_amount;
END; $$;
GRANT EXECUTE ON FUNCTION credit_topup_grant(UUID,UUID,INTEGER,TEXT,TIMESTAMPTZ,JSONB) TO app_user;

-- credit_reset_subscription: the bucket-aware RESET (replaces credit_grant_capped
-- cap=0 for the global rail). Sets sub portion = p_allowance; KEEPS the topup bucket
-- unless it is already past its own expiry (normal lapse) OR p_forfeit_topup=true
-- (refund/abuse only — NOT used on subscription.expired: a paid top-up outlives a
-- sub lapse). balance = allowance + surviving topup. Idempotent on op_id. Returns
-- the SIGNED delta for the Redis mirror.
CREATE OR REPLACE FUNCTION credit_reset_subscription(
    p_tenant UUID, p_user UUID, p_allowance INTEGER, p_forfeit_topup BOOLEAN,
    p_op_id TEXT, p_reason TEXT DEFAULT 'monthly_grant', p_metadata JSONB DEFAULT '{}'::jsonb
) RETURNS TABLE(applied BOOLEAN, balance BIGINT, delta INTEGER)
LANGUAGE plpgsql AS $$
DECLARE v_cur BIGINT; v_topup BIGINT; v_topexp TIMESTAMPTZ; v_keep BIGINT; v_target BIGINT; v_delta INTEGER; v_ledger_id UUID; v_balance BIGINT;
BEGIN
    IF p_allowance IS NULL OR p_allowance < 0 THEN RAISE EXCEPTION 'allowance must be >= 0 (got %)', p_allowance; END IF;
    IF p_op_id IS NULL OR length(p_op_id) = 0 THEN RAISE EXCEPTION 'credit_reset_subscription requires an op_id'; END IF;
    SELECT cb.balance, cb.topup_balance, cb.topup_expires_at INTO v_cur, v_topup, v_topexp
      FROM credit_balances cb WHERE cb.tenant_id = p_tenant FOR UPDATE;
    v_cur := COALESCE(v_cur, 0); v_topup := COALESCE(v_topup, 0);
    IF p_forfeit_topup OR (v_topexp IS NOT NULL AND v_topexp <= now()) THEN
        v_keep := 0;                              -- forced forfeit, or topup already expired
    ELSE
        v_keep := v_topup;                        -- paid top-up survives (incl. through a sub lapse)
    END IF;
    v_target := p_allowance::BIGINT + v_keep;
    v_delta  := (v_target - v_cur)::INTEGER;
    INSERT INTO credit_ledger (tenant_id, user_id, delta, reason, op_id, metadata)
    VALUES (p_tenant, p_user, v_delta, p_reason, p_op_id,
            COALESCE(p_metadata,'{}'::jsonb) || jsonb_build_object(
                'reset', true, 'sub_target', p_allowance, 'topup_kept', v_keep, 'previous_balance', v_cur))
    ON CONFLICT (tenant_id, op_id) WHERE op_id IS NOT NULL DO NOTHING
    RETURNING id INTO v_ledger_id;
    IF v_ledger_id IS NULL THEN
        RETURN QUERY SELECT false, v_cur, 0; RETURN;
    END IF;
    INSERT INTO credit_balances (tenant_id, balance, topup_balance, topup_expires_at)
    VALUES (p_tenant, v_target, v_keep, CASE WHEN v_keep = 0 THEN NULL ELSE v_topexp END)
    ON CONFLICT (tenant_id) DO UPDATE SET
        balance          = EXCLUDED.balance,
        topup_balance    = EXCLUDED.topup_balance,
        topup_expires_at = EXCLUDED.topup_expires_at,
        updated_at       = now()
    RETURNING credit_balances.balance INTO v_balance;
    UPDATE credit_ledger SET balance_after = v_balance WHERE id = v_ledger_id;
    RETURN QUERY SELECT true, v_balance, v_delta;
END; $$;
GRANT EXECUTE ON FUNCTION credit_reset_subscription(UUID,UUID,INTEGER,BOOLEAN,TEXT,TEXT,JSONB) TO app_user;

-- credit_sweep_expired_topup (SECURITY DEFINER, cross-tenant): the MANDATORY Free
-- fallback. Free users have no renewal event to sweep their expired top-up, so this
-- daily cron zeroes any topup_balance whose topup_expires_at has passed (any tenant —
-- also a safety net for paid drift). Per-row it sets the tenant context, writes a
-- 'lapse' ledger line (idempotent op_id), drops the topup from balance, and clears
-- the bucket. Returns the affected rows so the Node cron can mirror Redis.
CREATE OR REPLACE FUNCTION credit_sweep_expired_topup(p_limit INTEGER DEFAULT 500)
RETURNS TABLE(tenant_id UUID, forfeited BIGINT, new_balance BIGINT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
#variable_conflict use_column
DECLARE r RECORD; v_prev TEXT; v_bal BIGINT; v_topup BIGINT; v_exp TIMESTAMPTZ; v_forfeit BIGINT;
BEGIN
    v_prev := current_setting('app.current_tenant_id', true);
    -- Candidate scan is snapshot-only (no lock); each row is then RE-READ under FOR
    -- UPDATE and the LIVE topup_balance is used. This closes two races the audit
    -- found: (1) a concurrent charge consuming top-up between SELECT and UPDATE
    -- (stale snapshot would over-debit), and (2) a concurrent renewal-reset already
    -- zeroing the bucket (would double-forfeit). The op_id carries the forfeited
    -- amount so a re-grant→re-sweep at the same expiry-second can't collide.
    FOR r IN
        SELECT cb.tenant_id AS tid
          FROM credit_balances cb
         WHERE cb.topup_balance > 0 AND cb.topup_expires_at IS NOT NULL AND cb.topup_expires_at <= now()
         ORDER BY cb.topup_expires_at ASC
         LIMIT p_limit
    LOOP
        PERFORM set_config('app.current_tenant_id', r.tid::text, true);
        -- Re-read LIVE under lock; skip if a concurrent charge/reset already cleared it.
        SELECT cb.topup_balance, cb.balance, cb.topup_expires_at INTO v_topup, v_bal, v_exp
          FROM credit_balances cb
         WHERE cb.tenant_id = r.tid AND cb.topup_balance > 0
           AND cb.topup_expires_at IS NOT NULL AND cb.topup_expires_at <= now()
         FOR UPDATE;
        IF NOT FOUND THEN CONTINUE; END IF;
        v_forfeit := LEAST(v_topup, GREATEST(v_bal, 0));   -- never debit more than the live total
        INSERT INTO credit_ledger (tenant_id, user_id, delta, reason, op_id, metadata)
        VALUES (r.tid, NULL, -v_forfeit, 'lapse',
                'topup_lapse:'||r.tid::text||':'||floor(extract(epoch from v_exp))::text||':'||v_forfeit::text,
                jsonb_build_object('bucket','topup','reason','expired','expired_at', v_exp))
        ON CONFLICT (tenant_id, op_id) WHERE op_id IS NOT NULL DO NOTHING;
        UPDATE credit_balances
           SET balance = balance - v_forfeit, topup_balance = 0, topup_expires_at = NULL, updated_at = now()
         WHERE credit_balances.tenant_id = r.tid
         RETURNING balance INTO v_bal;
        tenant_id := r.tid; forfeited := v_forfeit; new_balance := v_bal;
        RETURN NEXT;
    END LOOP;
    PERFORM set_config('app.current_tenant_id', COALESCE(v_prev,''), true);
END; $$;
REVOKE ALL ON FUNCTION credit_sweep_expired_topup(INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION credit_sweep_expired_topup(INTEGER) TO app_user;

COMMIT;
