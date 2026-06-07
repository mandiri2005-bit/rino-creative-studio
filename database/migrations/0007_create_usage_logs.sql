-- =====================================================================
-- 0007_create_usage_logs.sql
-- Per-call LLM usage tracking for billing and analytics
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS usage_logs (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id             UUID        REFERENCES users(id) ON DELETE SET NULL,
    session_id          UUID        REFERENCES chat_sessions(id) ON DELETE SET NULL,
    job_id              UUID        REFERENCES jobs(id) ON DELETE SET NULL,

    -- ── What was called ──────────────────────────────────────────────────
    endpoint            TEXT        NOT NULL
                            CHECK (endpoint IN ('chat', 'image', 'tts', 'video',
                                               'embedding', 'batch', 'other')),
    model_alias         TEXT        NOT NULL,
    model_upstream      TEXT        NOT NULL,
    provider            TEXT        NOT NULL
                            CHECK (provider IN ('laozhang', 'deepseek', 'gemini',
                                               'openai', 'other')),

    -- ── Token & cost accounting ──────────────────────────────────────────
    tokens_in           INTEGER     NOT NULL DEFAULT 0,
    tokens_out          INTEGER     NOT NULL DEFAULT 0,
    cost_usd            NUMERIC(12, 8) NOT NULL DEFAULT 0,

    -- ── Optional quality signals ─────────────────────────────────────────
    finish_reason       TEXT,
    latency_ms          INTEGER,
    http_status         SMALLINT,

    -- ── Timestamps ───────────────────────────────────────────────────────
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Indexes ─────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_usage_logs_tenant_id   ON usage_logs (tenant_id);
CREATE INDEX IF NOT EXISTS idx_usage_logs_user_id     ON usage_logs (user_id);
CREATE INDEX IF NOT EXISTS idx_usage_logs_job_id      ON usage_logs (job_id) WHERE job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_usage_logs_session_id  ON usage_logs (session_id) WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_usage_logs_created_at  ON usage_logs (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_logs_endpoint    ON usage_logs (tenant_id, endpoint, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_logs_model       ON usage_logs (tenant_id, model_upstream);

-- ── Comments ────────────────────────────────────────────────────────────────
COMMENT ON TABLE  usage_logs              IS 'One row per upstream LLM/image/TTS/video API call. Captures tokens and cost for billing dashboards.';
COMMENT ON COLUMN usage_logs.model_alias  IS 'User-facing model alias (e.g. "gemini-2.5-pro") before MODELS dict resolution.';
COMMENT ON COLUMN usage_logs.model_upstream IS 'Resolved upstream model string actually sent to the provider.';
COMMENT ON COLUMN usage_logs.cost_usd     IS 'Computed by the application layer using per-model rate tables; 8 decimal places for sub-cent precision.';

-- ── Row-Level Security ──────────────────────────────────────────────────────
ALTER TABLE usage_logs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON usage_logs;
CREATE POLICY tenant_isolation ON usage_logs
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

-- ── updated_at trigger ──────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_usage_logs_updated_at ON usage_logs;
CREATE TRIGGER trg_usage_logs_updated_at
    BEFORE UPDATE ON usage_logs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMIT;
