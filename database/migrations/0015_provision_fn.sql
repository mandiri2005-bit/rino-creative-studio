-- 0015_provision_fn.sql
-- With FORCE RLS (0011), the Clerk webhook can no longer INSERT a brand-new
-- tenant before a tenant context exists. This SECURITY DEFINER function does the
-- whole provisioning atomically, bypassing RLS safely. Stripe columns use the
-- same 'free' placeholders the webhook already used (real values arrive later
-- via the Stripe webhook in Phase 4).

BEGIN;

CREATE OR REPLACE FUNCTION provision_tenant(
  p_tenant_id   uuid,
  p_name        text,
  p_slug        text,
  p_email       text,
  p_plan        text,
  p_clerk_user  text,
  p_role        text DEFAULT 'admin'
) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE v_tenant uuid;
BEGIN
  INSERT INTO tenants (id, name, slug, email, plan)
       VALUES (COALESCE(p_tenant_id, gen_random_uuid()), p_name, p_slug, p_email, p_plan)
  ON CONFLICT (id) DO UPDATE SET updated_at = now()
  RETURNING id INTO v_tenant;

  IF v_tenant IS NULL THEN
    SELECT id INTO v_tenant FROM tenants WHERE email = p_email LIMIT 1;
  END IF;

  INSERT INTO users (tenant_id, email, display_name, external_id, role)
       VALUES (v_tenant, p_email, p_name, p_clerk_user, p_role)
  ON CONFLICT (tenant_id, email) DO UPDATE SET external_id = EXCLUDED.external_id;

  INSERT INTO subscriptions (
         tenant_id, stripe_customer_id, stripe_subscription_id,
         stripe_price_id, stripe_product_id, plan, status,
         current_period_start, current_period_end)
       VALUES (
         v_tenant, 'cus_free_' || p_clerk_user, 'sub_free_' || p_clerk_user,
         'price_free', 'prod_free', p_plan, 'active',
         now(), now() + interval '1 year')
  ON CONFLICT (stripe_subscription_id) DO NOTHING;

  RETURN v_tenant;
END $$;

REVOKE ALL ON FUNCTION provision_tenant(uuid,text,text,text,text,text,text) FROM PUBLIC;

COMMIT;
