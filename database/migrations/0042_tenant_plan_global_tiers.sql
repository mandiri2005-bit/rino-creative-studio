-- =====================================================================
-- 0042_tenant_plan_global_tiers.sql
-- Widen the tenants.plan CHECK to admit the GLOBAL subscription ladder.
--
-- 0001 defined tenants.plan CHECK (plan IN ('free','starter','pro','enterprise')) —
-- the Indonesia 4-tier ladder. The GLOBAL deployment's plan keys are
-- free|starter|plus|pro|ultra; when a Plus/Ultra subscription activates, the
-- webhook's _setTenantPlan does `UPDATE tenants SET plan='plus'|'ultra'`, which
-- VIOLATED the old CHECK and threw (silently swallowed → tenants.plan stayed stale →
-- the spend-gate read the wrong tier and locked a paying Plus/Ultra user out of
-- their models). This widens the constraint to the UNION of both rails' plan names
-- so the same shared codebase serves both deployments. Additive + idempotent; the
-- Indonesia values remain valid, so this is a no-op there.
-- =====================================================================

BEGIN;

ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_plan_check;
ALTER TABLE tenants ADD  CONSTRAINT tenants_plan_check
    CHECK (plan IN ('free','starter','plus','pro','ultra','enterprise'));

COMMENT ON CONSTRAINT tenants_plan_check ON tenants IS
  'Union of both rails: Indonesia (free/starter/pro/enterprise) + Global subscription (free/starter/plus/pro/ultra). tenants.plan is the spend-gate tier source (_tier_for).';

COMMIT;
