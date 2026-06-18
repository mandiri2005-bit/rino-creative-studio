-- 0029_usage_gl_tagging.sql
-- ============================================================================
-- Financial reporting groundwork — clean from day one, LIGHTWEIGHT (no journal).
--
--  (b) BUGFIX: usage_logs.endpoint CHECK rejected the Video-Instant variants the
--      code already writes (metering.py: '<ep>-VI'), so every Video-Instant usage
--      row failed the constraint and was silently swallowed → its revenue + COGS
--      were missing from ALL reporting. Widen the CHECK to accept them.
--
--  (a) GL TAGGING: add denormalized money columns to usage_logs so every
--      consumption row is self-describing for P&L + per-op gross margin via a
--      simple GROUP BY. Populated automatically in database.log_usage() at write
--      time (kurs + sale price + gl codes from credit_catalog). NO double-entry
--      journal table yet — defer that to PKP / fundraise (see GL design notes).
--
-- Idempotent. Reporting views are RLS-bound to usage_logs/credit_balances; run
-- the GLOBAL financial views as the DB owner (bypasses RLS) for whole-business P&L.
-- ============================================================================
BEGIN;

-- (b) Allow the Video-Instant endpoint variants the app already emits ------------
ALTER TABLE usage_logs DROP CONSTRAINT IF EXISTS usage_logs_endpoint_check;
ALTER TABLE usage_logs ADD  CONSTRAINT usage_logs_endpoint_check
    CHECK (endpoint IN ('chat','image','tts','video','embedding','batch','narasi','other',
                        'chat-VI','image-VI','tts-VI','video-VI'));

-- (a) GL-tag columns (all nullable; historical rows stay NULL, views COALESCE) ----
ALTER TABLE usage_logs
    ADD COLUMN IF NOT EXISTS cost_idr        NUMERIC(14,2),  -- COGS in IDR = cost_usd * kurs (at write)
    ADD COLUMN IF NOT EXISTS revenue_idr     NUMERIC(14,2),  -- recognized revenue (paid only) = credits * sale_price
    ADD COLUMN IF NOT EXISTS markup_factor   NUMERIC(6,3),   -- effective markup applied (audit; durable vs ENV drift)
    ADD COLUMN IF NOT EXISTS is_paid         BOOLEAN,        -- funding source: paid=revenue, free=acquisition cost
    ADD COLUMN IF NOT EXISTS gl_revenue_code TEXT,           -- 41xx revenue account (by op-type)
    ADD COLUMN IF NOT EXISTS gl_cogs_code    TEXT;           -- 51xx COGS account (by op-type)

-- Lightweight chart-of-accounts REFERENCE (names only, NOT a journal) -------------
CREATE TABLE IF NOT EXISTS gl_accounts (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('asset','liability','equity','revenue','cogs','opex'))
);
INSERT INTO gl_accounts (code,name,type) VALUES
    ('1000','Kas & Bank','asset'),
    ('2000','Pendapatan Diterima Dimuka - Kredit (deferred revenue)','liability'),
    ('2100','Utang / Prepaid Provider Upstream','liability'),
    ('4100','Pendapatan - Image','revenue'),
    ('4200','Pendapatan - Video','revenue'),
    ('4300','Pendapatan - Chat/Text','revenue'),
    ('4400','Pendapatan - TTS/Audio','revenue'),
    ('4500','Pendapatan - Lain (narasi/embedding/other)','revenue'),
    ('5100','COGS - Image','cogs'),
    ('5200','COGS - Video','cogs'),
    ('5300','COGS - Chat/Text','cogs'),
    ('5400','COGS - TTS/Audio','cogs'),
    ('5500','COGS - Lain','cogs'),
    ('6100','Biaya Akuisisi - Free Tier','opex'),
    ('6200','Biaya Payment Processing','opex'),
    ('6300','Selisih Kurs (FX)','opex')
ON CONFLICT (code) DO NOTHING;

-- ── Reporting views (query-time, no posted journal) ─────────────────────────────
-- Per-op gross margin (paid consumption only).
CREATE OR REPLACE VIEW v_op_margin AS
SELECT split_part(endpoint,'-',1)                          AS op,
       count(*)                                            AS ops,
       coalesce(sum(revenue_idr),0)                        AS revenue_idr,
       coalesce(sum(cost_idr),0)                           AS cogs_idr,
       coalesce(sum(revenue_idr),0) - coalesce(sum(cost_idr),0) AS gross_profit_idr,
       round(100 * (coalesce(sum(revenue_idr),0) - coalesce(sum(cost_idr),0))
             / nullif(sum(revenue_idr),0), 1)              AS margin_pct
FROM usage_logs
WHERE is_paid IS TRUE
GROUP BY 1;

-- Monthly P&L: revenue, COGS (paid), free-tier acquisition cost, gross profit.
CREATE OR REPLACE VIEW v_pl_monthly AS
SELECT date_trunc('month', created_at)::date                       AS month,
       coalesce(sum(revenue_idr) FILTER (WHERE is_paid), 0)        AS revenue_idr,
       coalesce(sum(cost_idr)    FILTER (WHERE is_paid), 0)        AS cogs_idr,
       coalesce(sum(cost_idr)    FILTER (WHERE is_paid IS NOT TRUE), 0) AS free_acq_cost_idr,
       coalesce(sum(revenue_idr) FILTER (WHERE is_paid), 0)
         - coalesce(sum(cost_idr) FILTER (WHERE is_paid), 0)       AS gross_profit_idr
FROM usage_logs
GROUP BY 1
ORDER BY 1;

-- COGS by upstream provider (drives provider-payable reconciliation + FX exposure).
CREATE OR REPLACE VIEW v_cogs_by_provider AS
SELECT date_trunc('month', created_at)::date AS month, provider,
       coalesce(sum(cost_usd),0) AS cogs_usd,
       coalesce(sum(cost_idr),0) AS cogs_idr,
       count(*)                  AS ops
FROM usage_logs
GROUP BY 1,2
ORDER BY 1,2;

-- Revenue by GL account (named).
CREATE OR REPLACE VIEW v_revenue_by_gl AS
SELECT date_trunc('month', u.created_at)::date AS month,
       u.gl_revenue_code AS code, a.name,
       coalesce(sum(u.revenue_idr),0) AS revenue_idr
FROM usage_logs u
LEFT JOIN gl_accounts a ON a.code = u.gl_revenue_code
WHERE u.is_paid IS TRUE
GROUP BY 1,2,3
ORDER BY 1,2;

-- Deferred-revenue liability = outstanding PAID credits × the plan's ACTUAL price/credit
-- (price/allowance), so it reconciles to cash collected — NOT a flat blended rate (E4 fix).
CREATE OR REPLACE VIEW v_deferred_revenue AS
SELECT coalesce(sum(b.balance),0)::bigint AS outstanding_credits,
       round(coalesce(sum(b.balance * CASE t.plan
              WHEN 'starter'    THEN 246.875
              WHEN 'pro'        THEN 234.1176
              WHEN 'enterprise' THEN 199.6
              ELSE 248 END), 0), 2)        AS deferred_revenue_idr
FROM credit_balances b
JOIN tenants t ON t.id = b.tenant_id
WHERE t.plan <> 'free';

GRANT SELECT ON gl_accounts        TO app_user;
GRANT SELECT ON v_op_margin        TO app_user;
GRANT SELECT ON v_pl_monthly       TO app_user;
GRANT SELECT ON v_cogs_by_provider TO app_user;
GRANT SELECT ON v_revenue_by_gl    TO app_user;
GRANT SELECT ON v_deferred_revenue TO app_user;

COMMIT;
