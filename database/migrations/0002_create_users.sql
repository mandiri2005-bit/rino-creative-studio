-- =====================================================================
-- 0002_create_users.sql
-- Users table — one row per human user, scoped to a tenant
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS users (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email               TEXT        NOT NULL,
    display_name        TEXT,
    password_hash       TEXT,
    external_id         TEXT,
    role                TEXT        NOT NULL DEFAULT 'member'
                            CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    is_active           BOOLEAN     NOT NULL DEFAULT TRUE,
    last_login_at       TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (tenant_id, email)
);

-- ── Indexes ─────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_users_tenant_id   ON users (tenant_id);
CREATE INDEX IF NOT EXISTS idx_users_email       ON users (email);
CREATE INDEX IF NOT EXISTS idx_users_external_id ON users (external_id) WHERE external_id IS NOT NULL;

-- ── Comments ────────────────────────────────────────────────────────────────
COMMENT ON TABLE  users               IS 'One row per human user, always scoped to a tenant.';
COMMENT ON COLUMN users.password_hash IS 'bcrypt/argon2 hash; NULL for SSO-only accounts.';
COMMENT ON COLUMN users.external_id   IS 'OAuth sub-claim or SAML nameID for federated identity.';

-- ── Row-Level Security ──────────────────────────────────────────────────────
ALTER TABLE users ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON users;
CREATE POLICY tenant_isolation ON users
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

-- ── updated_at trigger ──────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_users_updated_at ON users;
CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMIT;
