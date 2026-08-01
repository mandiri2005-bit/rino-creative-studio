-- =====================================================================
-- 0071_deferred_revenue_no_silent_fallback.sql
--
-- Closes §17.1 of WIMBA-ACCOUNTING-TAX-INVESTOR-READINESS-AUDIT-2026-08-01.
--
-- THE DEFECT (active in production at the time of writing, not hypothetical):
--   v_deferred_revenue (0029:110-119) prices outstanding credits from hardcoded SQL
--   literals with a catch-all `ELSE 248`:
--       WHEN 'starter' THEN 246.875  WHEN 'pro' THEN 234.1176
--       WHEN 'enterprise' THEN 199.6  ELSE 248
--   Four sources disagree about what a plan costs:
--       this view          starter / pro / enterprise            + ELSE 248
--       config/pricing.json  tier_price_idr: starter/pro/enterprise
--       Dodo products      STARTER / PRO / PLUS / ULTRA
--       tenants.plan CHECK free/starter/plus/pro/ultra/enterprise   (0042:20)
--   `plus` and `ultra` are sold but appear in NEITHER the view NOR pricing.json, so they
--   fall through to 248 — a constant that is nobody's price.
--
--   Measured in production 2026-08-01: the only tenant is on plan `ultra`, so
--       SELECT * FROM v_deferred_revenue;  ->  99987 credits, Rp 24,796,776.00
--   i.e. 99987 x 248. A fabricated liability, reported with no indication anything is wrong.
--
-- WHAT THIS MIGRATION DOES — and deliberately does NOT do:
--   It makes the view REFUSE TO GUESS. Unknown plans are priced NULL instead of 248, and
--   their credits are reported separately as `unpriced_credits` with `is_complete = false`.
--   Silent wrongness becomes loud incompleteness.
--
--   It does NOT invent prices for `plus` and `ultra`. Authoritative IDR per-credit figures
--   for those tiers do not exist anywhere in the repo, and fabricating them here would add a
--   FIFTH disagreeing source — the exact failure being fixed. Whoever owns pricing supplies
--   them, or the F-02 credit-lot work makes the question moot.
--
-- EXPECTED IMPACT ON THE LIVE NUMBER: deferred_revenue_idr goes 24,796,776.00 -> 0.00, with
--   unpriced_credits = 99987 and is_complete = false. That is the point. The old number was
--   not smaller or larger than the truth; it was unfounded. Zero-with-a-reason is a correct
--   report of "this cannot be computed yet"; Rp 24.8M was not.
--
-- BACKWARD COMPATIBILITY: `outstanding_credits` and `deferred_revenue_idr` keep their names,
--   types and ordinal positions; the three new columns are appended, so CREATE OR REPLACE
--   works and existing grants survive. No in-repo caller exists (fixed-string search over all
--   tracked files: zero hits outside database/migrations), but the separate admin console is
--   a different repository and was NOT searched — if it renders this figure, it must be
--   re-checked against `is_complete` before the number is shown to anyone.
--
-- STILL BROKEN AFTER THIS MIGRATION (do not read this as "deferred revenue now works"):
--   1. The three surviving literals belong to the Indonesia tier regime. Production runs
--      BILLING_MODE=subscription on the global tiers, where credit_catalog.py:393 derives
--      starter at ~Rp35.96/credit — roughly 7x below the 246.875 here (audit F-02.b). So on
--      the live deployment these plans would be mispriced too, if any tenant used them.
--   2. `WHERE t.plan <> 'free'` still drops lapsed subscribers whose PAID top-up credits
--      deliberately survive the lapse (payments_core.mjs:367-368). Those credits produce no
--      liability here and no revenue on consumption. Fixing that needs paid-vs-free lot
--      provenance, i.e. credit_lots — audit F-02. It is NOT fixed here, and papering over it
--      would hide it again.
--   This view remains a query-time estimate. It is not a deferred-revenue rollforward and
--   must not be used as one, or reported to an investor, until F-01/F-02 land.
--
-- REVERSAL (forward-only repo; recorded here rather than shipped as a down-migration):
--   re-run the 0029:110-119 definition verbatim, then DROP VIEW v_deferred_revenue_by_plan.
--   Reversal is lossless — a view carries no data, and no table is touched.
--
-- VERIFICATION PERFORMED (local PostgreSQL 18.4, all 71 migrations applied, not production).
--   Seeded one `ultra` tenant with 99987 credits and one `starter` with 1000, then compared:
--     old definition : Rp 25,043,651.00   (both tenants priced, ultra silently at 248)
--     new definition : outstanding 100987 | revenue Rp 246,875.00 | priced 1000 |
--                      unpriced 99987 | is_complete = false
--     by_plan        : starter -> 246.875 (is_unpriced=false); ultra -> NULL (is_unpriced=true)
--   The fabricated Rp 24.8M measured in production comes from exactly this path.
-- =====================================================================

BEGIN;

CREATE OR REPLACE VIEW v_deferred_revenue AS
WITH scoped AS (
    SELECT b.balance,
           t.plan,
           -- Indonesia-regime literals carried over verbatim from 0029:112-116.
           -- No ELSE branch: an unrecognised plan prices to NULL, never to a fallback.
           CASE t.plan
               WHEN 'starter'    THEN 246.875::numeric
               WHEN 'pro'        THEN 234.1176::numeric
               WHEN 'enterprise' THEN 199.6::numeric
               ELSE NULL::numeric
           END AS price_idr_per_credit
    FROM credit_balances b
    JOIN tenants t ON t.id = b.tenant_id
    WHERE t.plan <> 'free'
)
SELECT
    COALESCE(SUM(balance), 0)::bigint                                        AS outstanding_credits,
    ROUND(COALESCE(SUM(balance * price_idr_per_credit)
                   FILTER (WHERE price_idr_per_credit IS NOT NULL), 0), 2)   AS deferred_revenue_idr,
    COALESCE(SUM(balance) FILTER (WHERE price_idr_per_credit IS NOT NULL), 0)::bigint AS priced_credits,
    COALESCE(SUM(balance) FILTER (WHERE price_idr_per_credit IS NULL),     0)::bigint AS unpriced_credits,
    (COALESCE(SUM(balance) FILTER (WHERE price_idr_per_credit IS NULL), 0) = 0)       AS is_complete
FROM scoped;

COMMENT ON VIEW v_deferred_revenue IS
  'Query-time ESTIMATE of the outstanding credit liability — NOT a deferred-revenue '
  'rollforward and not investor-reportable (audit F-01/F-02). As of 0071 an unrecognised '
  'plan prices to NULL rather than a 248 fallback: read is_complete FIRST, and treat '
  'deferred_revenue_idr as meaningless whenever unpriced_credits > 0. Plans plus/ultra are '
  'sold but have no IDR per-credit price anywhere in the repo, so today they are always '
  'unpriced. Superseded once credit_lots carries real consideration per lot.';

-- ---------------------------------------------------------------------
-- Diagnostic companion: shows WHICH plans are unpriced, so the gap is actionable rather
-- than just visible. New view, so no compatibility surface to preserve.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_deferred_revenue_by_plan AS
SELECT t.plan,
       count(*)::bigint                        AS tenants,
       COALESCE(SUM(b.balance), 0)::bigint     AS outstanding_credits,
       CASE t.plan
           WHEN 'starter'    THEN 246.875::numeric
           WHEN 'pro'        THEN 234.1176::numeric
           WHEN 'enterprise' THEN 199.6::numeric
           ELSE NULL::numeric
       END                                     AS price_idr_per_credit,
       (t.plan NOT IN ('starter','pro','enterprise')) AS is_unpriced
FROM credit_balances b
JOIN tenants t ON t.id = b.tenant_id
WHERE t.plan <> 'free'
GROUP BY t.plan
ORDER BY t.plan;

COMMENT ON VIEW v_deferred_revenue_by_plan IS
  'Per-plan breakdown behind v_deferred_revenue. is_unpriced = true means the plan is sold '
  'but has no IDR per-credit price in the schema (currently plus and ultra). Use this to see '
  'which pricing gap is producing an incomplete liability figure.';

GRANT SELECT ON v_deferred_revenue_by_plan TO app_user;

COMMIT;
