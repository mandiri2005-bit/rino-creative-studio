-- =====================================================================
-- 0003_create_api_keys.sql
-- Encrypted upstream API credentials per tenant
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS api_keys (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider            TEXT        NOT NULL
                            CHECK (provider IN ('laozhang', 'laozhang_image',
                                               'deepseek', 'gemini', 'openai', 'other')),
    label               TEXT        NOT NULL DEFAULT '',
    key_value_enc       BYTEA       NOT NULL,
    key_hint            TEXT        GENERATED ALWAYS AS ('***') STORED,
    is_active           BOOLEAN     NOT NULL DEFAULT TRUE,
    last_used_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Indexes ─────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant_id ON api_keys (tenant_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_provider  ON api_keys (tenant_id, provider, is_active);

-- ── Comments ────────────────────────────────────────────────────────────────
COMMENT ON TABLE  api_keys              IS 'Encrypted upstream API credentials per tenant. Replaces shared env vars.';
COMMENT ON COLUMN api_keys.key_value_enc IS 'pgp_sym_encrypt(raw_key, app_secret) — never store plaintext.';
COMMENT ON COLUMN api_keys.key_hint     IS 'Last-4-character hint shown in UI; override at INSERT.';

-- ── Row-Level Security ──────────────────────────────────────────────────────
ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON api_keys;
CREATE POLICY tenant_isolation ON api_keys
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

-- ── updated_at trigger ──────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_api_keys_updated_at ON api_keys;
CREATE TRIGGER trg_api_keys_updated_at
    BEFORE UPDATE ON api_keys
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMIT;
