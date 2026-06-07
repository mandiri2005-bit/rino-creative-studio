-- =====================================================================
-- 0001_create_tenants.sql
-- Prerequisites (extensions, helper function) + tenants table
-- =====================================================================

BEGIN;

-- ── Extensions ──────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid(), pgp_sym_encrypt
CREATE EXTENSION IF NOT EXISTS "pg_trgm";    -- fast LIKE / ILIKE on text columns

-- ── Shared helper: auto-set updated_at on every UPDATE ──────────────────────
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

-- ── Table ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tenants (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT        NOT NULL,
    slug                TEXT        NOT NULL UNIQUE,
    email               TEXT        NOT NULL UNIQUE,
    plan                TEXT        NOT NULL DEFAULT 'free'
                            CHECK (plan IN ('free', 'starter', 'pro', 'enterprise')),
    is_active           BOOLEAN     NOT NULL DEFAULT TRUE,
    settings            JSONB       NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Indexes ─────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_tenants_slug      ON tenants (slug);
CREATE INDEX IF NOT EXISTS idx_tenants_email     ON tenants (email);
CREATE INDEX IF NOT EXISTS idx_tenants_is_active ON tenants (is_active);

-- ── Comments ────────────────────────────────────────────────────────────────
COMMENT ON TABLE  tenants          IS 'One row per organisation or individual customer.';
COMMENT ON COLUMN tenants.slug     IS 'URL-safe short name, used as subdomain prefix.';
COMMENT ON COLUMN tenants.settings IS 'Per-tenant feature flags and UI preferences (free-form JSONB).';

-- ── Row-Level Security ──────────────────────────────────────────────────────
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON tenants;
CREATE POLICY tenant_isolation ON tenants
    USING (id = current_setting('app.current_tenant_id', TRUE)::UUID);

-- ── updated_at trigger ──────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_tenants_updated_at ON tenants;
CREATE TRIGGER trg_tenants_updated_at
    BEFORE UPDATE ON tenants
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMIT;
