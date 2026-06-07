-- =============================================================================
--  Rino Creative Studio — Multi-Tenant PostgreSQL Schema
--  PostgreSQL 15+  |  UUID primary keys  |  Row-Level Security ready
-- =============================================================================
--
--  Tables (9):
--    1.  tenants          – one row per organisation / individual customer
--    2.  users            – one row per user, belongs to a tenant
--    3.  api_keys         – encrypted upstream API keys per tenant
--    4.  chat_sessions    – replaces in-memory `sessions` dict (laozhang_api.py)
--    5.  chat_messages    – individual messages inside sessions
--    6.  jobs             – replaces _oneshot_jobs + jobs.json + tts-jobs.json
--                           + imagen-jobs.json + activeJobs Map (server.js)
--    7.  usage_logs       – every LLM API call with tokens + cost per tenant
--    8.  assets           – replaces Docker volume file references
--                           (Veo, Sora, TTS, image outputs)
--    9.  subscriptions    – mirrors Stripe subscription state per tenant
--
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 0.  Prerequisites
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid(), pgp_sym_encrypt
CREATE EXTENSION IF NOT EXISTS "pg_trgm";    -- fast LIKE / ILIKE on text columns


-- ---------------------------------------------------------------------------
-- 1.  tenants
-- ---------------------------------------------------------------------------
-- One row per paying customer (organisation or individual).
-- slug is used in subdomains or path prefixes (e.g. acme.rinocreative.app).
-- settings is a free-form JSONB bag for per-tenant feature flags and UI prefs.
-- ---------------------------------------------------------------------------
CREATE TABLE tenants (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT        NOT NULL,
    slug                TEXT        NOT NULL UNIQUE,          -- URL-safe identifier
    email               TEXT        NOT NULL UNIQUE,          -- billing contact
    plan                TEXT        NOT NULL DEFAULT 'free'
                            CHECK (plan IN ('free', 'starter', 'pro', 'enterprise')),
    is_active           BOOLEAN     NOT NULL DEFAULT TRUE,
    settings            JSONB       NOT NULL DEFAULT '{}',    -- feature flags, UI prefs
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_tenants_slug      ON tenants (slug);
CREATE INDEX idx_tenants_email     ON tenants (email);
CREATE INDEX idx_tenants_is_active ON tenants (is_active);

COMMENT ON TABLE  tenants          IS 'One row per organisation or individual customer.';
COMMENT ON COLUMN tenants.slug     IS 'URL-safe short name, used as subdomain prefix.';
COMMENT ON COLUMN tenants.settings IS 'Per-tenant feature flags and UI preferences (free-form JSONB).';


-- ---------------------------------------------------------------------------
-- 2.  users
-- ---------------------------------------------------------------------------
-- One row per human user.  A user belongs to exactly one tenant.
-- password_hash stores bcrypt / argon2 output; external_id is the provider
-- sub-claim when using SSO / OAuth.
-- ---------------------------------------------------------------------------
CREATE TABLE users (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email               TEXT        NOT NULL,
    display_name        TEXT,
    password_hash       TEXT,                                 -- NULL for SSO-only accounts
    external_id         TEXT,                                 -- OAuth sub or SAML nameID
    role                TEXT        NOT NULL DEFAULT 'member'
                            CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    is_active           BOOLEAN     NOT NULL DEFAULT TRUE,
    last_login_at       TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (tenant_id, email)
);

CREATE INDEX idx_users_tenant_id   ON users (tenant_id);
CREATE INDEX idx_users_email       ON users (email);
CREATE INDEX idx_users_external_id ON users (external_id) WHERE external_id IS NOT NULL;

COMMENT ON TABLE  users               IS 'One row per human user, always scoped to a tenant.';
COMMENT ON COLUMN users.password_hash IS 'bcrypt/argon2 hash; NULL for SSO-only accounts.';
COMMENT ON COLUMN users.external_id   IS 'OAuth sub-claim or SAML nameID for federated identity.';


-- ---------------------------------------------------------------------------
-- 3.  api_keys
-- ---------------------------------------------------------------------------
-- Stores upstream API credentials (LaoZhang, DeepSeek, Gemini, etc.) per
-- tenant.  key_value is encrypted with pgp_sym_encrypt; never stored in
-- plaintext.  Multiple key rows of the same provider are supported so tenants
-- can rotate keys without downtime (is_active controls which one is used).
--
-- Replaces the LAOZHANG_API_KEY / DEEPSEEK_API_KEY / GEMINI_API_KEY env vars
-- that are currently shared globally in laozhang_api.py and server.js.
-- ---------------------------------------------------------------------------
CREATE TABLE api_keys (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider            TEXT        NOT NULL
                            CHECK (provider IN ('laozhang', 'laozhang_image',
                                               'deepseek', 'gemini', 'openai', 'other')),
    label               TEXT        NOT NULL DEFAULT '',      -- human nickname, e.g. "prod key"
    key_value_enc       BYTEA       NOT NULL,                 -- pgp_sym_encrypt(raw_key, secret)
    key_hint            TEXT        GENERATED ALWAYS AS (
                            '***' -- override at insert time with last-4 chars if desired
                        ) STORED,
    is_active           BOOLEAN     NOT NULL DEFAULT TRUE,
    last_used_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_api_keys_tenant_id ON api_keys (tenant_id);
CREATE INDEX idx_api_keys_provider  ON api_keys (tenant_id, provider, is_active);

COMMENT ON TABLE  api_keys              IS 'Encrypted upstream API credentials per tenant. Replaces shared env vars.';
COMMENT ON COLUMN api_keys.key_value_enc IS 'pgp_sym_encrypt(raw_key, app_secret) — never store plaintext.';
COMMENT ON COLUMN api_keys.key_hint     IS 'Last-4-character hint shown in UI; override at INSERT.';


-- ---------------------------------------------------------------------------
-- 4.  chat_sessions
-- ---------------------------------------------------------------------------
-- Replaces the in-memory `sessions: dict[str, Conversation]` dictionary in
-- laozhang_api.py (capped at MAX_SESSIONS=100, evicting the oldest on overflow).
-- Each row holds the configuration that was passed when the session was created:
-- model, system prompt, temperature, max_tokens.  With PostgreSQL the cap and
-- eviction logic can be replaced with a TTL-based cleanup job.
-- ---------------------------------------------------------------------------
CREATE TABLE chat_sessions (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id             UUID        REFERENCES users(id) ON DELETE SET NULL,
    title               TEXT,                                 -- optional human label
    model               TEXT        NOT NULL,                 -- e.g. "gemini-2.5-pro"
    system_prompt       TEXT        NOT NULL DEFAULT '',
    temperature         NUMERIC(4,3) NOT NULL DEFAULT 0.9
                            CHECK (temperature BETWEEN 0 AND 2),
    max_tokens          INTEGER     NOT NULL DEFAULT 8192,
    use_tools           BOOLEAN     NOT NULL DEFAULT FALSE,
    mcp_paths           TEXT,                                 -- comma-sep paths for MCP sidecar
    is_archived         BOOLEAN     NOT NULL DEFAULT FALSE,
    last_message_at     TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_chat_sessions_tenant_id        ON chat_sessions (tenant_id);
CREATE INDEX idx_chat_sessions_user_id          ON chat_sessions (user_id);
CREATE INDEX idx_chat_sessions_last_message_at  ON chat_sessions (tenant_id, last_message_at DESC);
CREATE INDEX idx_chat_sessions_is_archived      ON chat_sessions (tenant_id, is_archived);

COMMENT ON TABLE  chat_sessions             IS 'Persistent chat sessions. Replaces the in-memory sessions dict in laozhang_api.py.';
COMMENT ON COLUMN chat_sessions.mcp_paths   IS 'Comma-separated folder paths forwarded to the MCP file-search sidecar.';
COMMENT ON COLUMN chat_sessions.last_message_at IS 'Denormalised timestamp of the most recent message for efficient sorting.';


-- ---------------------------------------------------------------------------
-- 5.  chat_messages
-- ---------------------------------------------------------------------------
-- One row per turn (user or assistant) within a chat_session.
-- Replaces the `history: list[dict]` array inside each Conversation object.
-- tool_calls and tool_results store the JSON payloads for MCP tool-use turns.
-- token counts are filled by the usage event emitted at end of stream
-- ("[USAGE:{input:N, output:N}]" in laozhang_api.py).
-- ---------------------------------------------------------------------------
CREATE TABLE chat_messages (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    session_id          UUID        NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role                TEXT        NOT NULL
                            CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content             TEXT        NOT NULL DEFAULT '',
    tool_calls          JSONB,                                -- model-emitted tool invocations
    tool_results        JSONB,                                -- tool execution results fed back
    finish_reason       TEXT,                                 -- stop | length | tool_calls | error
    tokens_in           INTEGER,                              -- prompt tokens (from USAGE event)
    tokens_out          INTEGER,                              -- completion tokens
    sequence_number     INTEGER     NOT NULL,                 -- 1-based order within session
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_chat_messages_session_id ON chat_messages (session_id, sequence_number);
CREATE INDEX idx_chat_messages_tenant_id  ON chat_messages (tenant_id);
CREATE INDEX idx_chat_messages_created_at ON chat_messages (session_id, created_at);

COMMENT ON TABLE  chat_messages                 IS 'Individual message turns inside a chat session. Replaces history arrays in Conversation objects.';
COMMENT ON COLUMN chat_messages.tool_calls      IS 'JSONB array of tool_use blocks emitted by the model (MCP).';
COMMENT ON COLUMN chat_messages.sequence_number IS '1-based monotonic counter within the session for stable ordering.';


-- ---------------------------------------------------------------------------
-- 6.  jobs
-- ---------------------------------------------------------------------------
-- Unified job table replacing four separate persistence mechanisms:
--   • _oneshot_jobs dict            (laozhang_api.py — narasi one-shot fix)
--   • jobs.json                     (server.js — Gemini batch image generation)
--   • tts-jobs.json                 (server.js — Google/LaoZhang TTS)
--   • imagen-jobs.json              (server.js — Imagen sequential generation)
--   • activeJobs Map (in-memory)    (server.js — live progress state)
--
-- job_type drives validation of the payload column.
-- progress_current / progress_total replace job.progress / job.total.
-- result_payload holds the final structured output (fixed_book text, file list, etc.).
-- external_job_id stores the provider-assigned ID (Gemini batch name, Veo task_id, etc.).
-- ---------------------------------------------------------------------------
CREATE TYPE job_type_enum AS ENUM (
    'oneshot_fix',      -- narasi manuscript fix (laozhang_api.py)
    'batch_image',      -- Gemini batch image generation (server.js)
    'tts',              -- Text-to-Speech (Google or LaoZhang TTS, server.js)
    'imagen',           -- Imagen sequential image generation (server.js)
    'veo',              -- Veo video generation (laozhang_api.py)
    'sora'              -- Sora video generation (laozhang_api.py)
);

CREATE TYPE job_status_enum AS ENUM (
    'queued',
    'processing',
    'running',
    'cancelling',
    'cancelled',
    'done',
    'error'
);

CREATE TABLE jobs (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID            NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id             UUID            REFERENCES users(id) ON DELETE SET NULL,
    job_type            job_type_enum   NOT NULL,
    status              job_status_enum NOT NULL DEFAULT 'queued',

    -- ── Input configuration ─────────────────────────────────────────────
    model               TEXT,           -- resolved upstream model name
    input_payload       JSONB,          -- request params (prompts[], voice, speed, etc.)

    -- ── Live progress (replaces in-memory activeJobs Map) ───────────────
    progress_current    INTEGER         NOT NULL DEFAULT 0,
    progress_total      INTEGER         NOT NULL DEFAULT 0,
    progress_message    TEXT,           -- human-readable status like "AI membaca manuskrip…"
    logs                JSONB           NOT NULL DEFAULT '[]',   -- array of log strings

    -- ── Output ──────────────────────────────────────────────────────────
    result_payload      JSONB,          -- done: {fixed_book, files[], destFile, etc.}
    error_message       TEXT,           -- error: exception string

    -- ── External provider reference ──────────────────────────────────────
    external_job_id     TEXT,           -- Gemini batch name, Veo task_id, Sora job id
    output_prefix       TEXT,           -- file prefix used during generation

    -- ── Linkage ─────────────────────────────────────────────────────────
    session_id          UUID            REFERENCES chat_sessions(id) ON DELETE SET NULL,

    -- ── Timestamps ──────────────────────────────────────────────────────
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT now()
);

CREATE INDEX idx_jobs_tenant_id        ON jobs (tenant_id);
CREATE INDEX idx_jobs_user_id          ON jobs (user_id);
CREATE INDEX idx_jobs_status           ON jobs (tenant_id, status);
CREATE INDEX idx_jobs_job_type         ON jobs (tenant_id, job_type);
CREATE INDEX idx_jobs_external_job_id  ON jobs (external_job_id) WHERE external_job_id IS NOT NULL;
CREATE INDEX idx_jobs_created_at       ON jobs (tenant_id, created_at DESC);
CREATE INDEX idx_jobs_session_id       ON jobs (session_id) WHERE session_id IS NOT NULL;

COMMENT ON TABLE  jobs                  IS 'Unified job table replacing _oneshot_jobs dict, jobs.json, tts-jobs.json, imagen-jobs.json, and the activeJobs Map.';
COMMENT ON COLUMN jobs.input_payload    IS 'Serialised request parameters: prompts[], voice, speed, transcriptBody, model, aspectRatio, etc.';
COMMENT ON COLUMN jobs.result_payload   IS 'Serialised output: fixed_book text, array of {file, url} objects, destFile path, etc.';
COMMENT ON COLUMN jobs.external_job_id  IS 'Provider-assigned identifier: Gemini batch name, Veo task_id, Sora job id.';
COMMENT ON COLUMN jobs.logs             IS 'Ordered array of human-readable log strings mirroring job.logs[] in server.js.';


-- ---------------------------------------------------------------------------
-- 7.  usage_logs
-- ---------------------------------------------------------------------------
-- One row per LLM API call across all endpoints: chat, image, TTS, video.
-- Captures the resolved upstream model name, token counts from the USAGE
-- events emitted by laozhang_api.py ("[USAGE:{input:N, output:N}]") and
-- server.js, and a derived cost_usd for billing.
-- job_id is nullable because chat-stream calls have no associated job.
-- ---------------------------------------------------------------------------
CREATE TABLE usage_logs (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id             UUID        REFERENCES users(id) ON DELETE SET NULL,
    session_id          UUID        REFERENCES chat_sessions(id) ON DELETE SET NULL,
    job_id              UUID        REFERENCES jobs(id) ON DELETE SET NULL,

    -- ── What was called ──────────────────────────────────────────────────
    endpoint            TEXT        NOT NULL
                            CHECK (endpoint IN ('chat', 'image', 'tts', 'video',
                                               'embedding', 'batch', 'other')),
    model_alias         TEXT        NOT NULL,   -- user-facing alias, e.g. "gemini-2.5-pro"
    model_upstream      TEXT        NOT NULL,   -- resolved upstream name sent to the API
    provider            TEXT        NOT NULL
                            CHECK (provider IN ('laozhang', 'deepseek', 'gemini',
                                               'openai', 'other')),

    -- ── Token & cost accounting ──────────────────────────────────────────
    tokens_in           INTEGER     NOT NULL DEFAULT 0,
    tokens_out          INTEGER     NOT NULL DEFAULT 0,
    cost_usd            NUMERIC(12, 8) NOT NULL DEFAULT 0,   -- computed by app layer

    -- ── Optional quality signals ─────────────────────────────────────────
    finish_reason       TEXT,                                -- stop | length | error | cancelled
    latency_ms          INTEGER,                             -- wall-clock ms for the call
    http_status         SMALLINT,                            -- upstream HTTP status code

    -- ── Timestamps ───────────────────────────────────────────────────────
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_usage_logs_tenant_id    ON usage_logs (tenant_id);
CREATE INDEX idx_usage_logs_user_id      ON usage_logs (user_id);
CREATE INDEX idx_usage_logs_job_id       ON usage_logs (job_id) WHERE job_id IS NOT NULL;
CREATE INDEX idx_usage_logs_session_id   ON usage_logs (session_id) WHERE session_id IS NOT NULL;
CREATE INDEX idx_usage_logs_created_at   ON usage_logs (tenant_id, created_at DESC);
CREATE INDEX idx_usage_logs_endpoint     ON usage_logs (tenant_id, endpoint, created_at DESC);
CREATE INDEX idx_usage_logs_model        ON usage_logs (tenant_id, model_upstream);

COMMENT ON TABLE  usage_logs              IS 'One row per upstream LLM/image/TTS/video API call. Captures tokens and cost for billing dashboards.';
COMMENT ON COLUMN usage_logs.model_alias  IS 'User-facing model alias (e.g. "gemini-2.5-pro") before MODELS dict resolution.';
COMMENT ON COLUMN usage_logs.model_upstream IS 'Resolved upstream model string actually sent to the provider.';
COMMENT ON COLUMN usage_logs.cost_usd     IS 'Computed by the application layer using per-model rate tables; 8 decimal places for sub-cent precision.';


-- ---------------------------------------------------------------------------
-- 8.  assets
-- ---------------------------------------------------------------------------
-- Replaces all Docker volume file references:
--   • ./data/veo/     → job_type veo,    content_type video/mp4
--   • ./data/sora/    → job_type sora,   content_type video/mp4
--   • ./data/tts/     → job_type tts,    content_type audio/wav
--   • ./data/imgs/    → job_type imagen, content_type image/jpeg
--   • ./data/batch/   → job_type batch_image
--   • ./data/narasi_temp/ → job_type oneshot_fix
--
-- s3_key + bucket uniquely identify the object in object storage.
-- signed_url / signed_url_expires_at support pre-signed download links.
-- ---------------------------------------------------------------------------
CREATE TABLE assets (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id                 UUID        REFERENCES users(id) ON DELETE SET NULL,
    job_id                  UUID        REFERENCES jobs(id) ON DELETE SET NULL,

    -- ── Object storage coordinates ────────────────────────────────────────
    bucket                  TEXT        NOT NULL,             -- S3 bucket name
    s3_key                  TEXT        NOT NULL,             -- full object key within bucket
    original_filename       TEXT,                             -- original filename for downloads

    -- ── MIME & size ───────────────────────────────────────────────────────
    content_type            TEXT        NOT NULL,             -- e.g. video/mp4, audio/wav, image/jpeg
    size_bytes              BIGINT      NOT NULL DEFAULT 0,

    -- ── Asset classification ──────────────────────────────────────────────
    asset_type              TEXT        NOT NULL
                                CHECK (asset_type IN ('video', 'audio', 'image',
                                                      'document', 'archive', 'other')),
    source_job_type         job_type_enum,                    -- which pipeline produced this

    -- ── Pre-signed URL cache ──────────────────────────────────────────────
    signed_url              TEXT,                             -- cached pre-signed download URL
    signed_url_expires_at   TIMESTAMPTZ,                      -- expiry time of signed_url

    -- ── Metadata ─────────────────────────────────────────────────────────
    metadata                JSONB       NOT NULL DEFAULT '{}', -- duration, width, height, etc.
    is_deleted              BOOLEAN     NOT NULL DEFAULT FALSE,

    -- ── Timestamps ───────────────────────────────────────────────────────
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (bucket, s3_key)
);

CREATE INDEX idx_assets_tenant_id         ON assets (tenant_id);
CREATE INDEX idx_assets_job_id            ON assets (job_id) WHERE job_id IS NOT NULL;
CREATE INDEX idx_assets_user_id           ON assets (user_id);
CREATE INDEX idx_assets_asset_type        ON assets (tenant_id, asset_type);
CREATE INDEX idx_assets_source_job_type   ON assets (tenant_id, source_job_type);
CREATE INDEX idx_assets_signed_url_expiry ON assets (signed_url_expires_at)
                                          WHERE signed_url IS NOT NULL;
CREATE INDEX idx_assets_created_at        ON assets (tenant_id, created_at DESC);

COMMENT ON TABLE  assets                     IS 'File references in object storage, replacing all Docker volume paths (Veo, Sora, TTS, Imagen, batch).';
COMMENT ON COLUMN assets.s3_key              IS 'Full object key within the bucket, e.g. "tenants/{tid}/jobs/{jid}/output_001.wav".';
COMMENT ON COLUMN assets.signed_url          IS 'Cached pre-signed URL; regenerate when signed_url_expires_at < now().';
COMMENT ON COLUMN assets.metadata            IS 'Free-form JSONB: video duration, image width/height, TTS voice, sample rate, etc.';


-- ---------------------------------------------------------------------------
-- 9.  subscriptions
-- ---------------------------------------------------------------------------
-- Mirrors Stripe subscription state per tenant.  One active row per tenant
-- at any given time (previous rows are retained for audit / proration).
-- stripe_* columns store the Stripe object IDs needed for webhook reconciliation
-- and portal redirects.  current_period_* are the billing cycle boundaries;
-- cancel_at is set when the customer requests a future cancellation.
-- ---------------------------------------------------------------------------
CREATE TABLE subscriptions (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- ── Stripe identifiers ────────────────────────────────────────────────
    stripe_customer_id      TEXT        NOT NULL,
    stripe_subscription_id  TEXT        NOT NULL UNIQUE,
    stripe_price_id         TEXT        NOT NULL,
    stripe_product_id       TEXT        NOT NULL,

    -- ── Plan / status ─────────────────────────────────────────────────────
    plan                    TEXT        NOT NULL
                                CHECK (plan IN ('free', 'starter', 'pro', 'enterprise')),
    status                  TEXT        NOT NULL
                                CHECK (status IN (
                                    'trialing', 'active', 'past_due', 'unpaid',
                                    'cancelled', 'incomplete', 'incomplete_expired',
                                    'paused'
                                )),

    -- ── Billing cycle ─────────────────────────────────────────────────────
    current_period_start    TIMESTAMPTZ NOT NULL,
    current_period_end      TIMESTAMPTZ NOT NULL,
    trial_start             TIMESTAMPTZ,
    trial_end               TIMESTAMPTZ,
    cancel_at               TIMESTAMPTZ,            -- scheduled future cancellation
    cancelled_at            TIMESTAMPTZ,            -- actual cancellation moment
    ended_at                TIMESTAMPTZ,            -- subscription fully ended

    -- ── Usage-based limits (denormalised from Stripe metadata) ────────────
    monthly_token_limit     BIGINT,                 -- NULL = unlimited
    monthly_job_limit       INTEGER,                -- NULL = unlimited
    seats                   SMALLINT    NOT NULL DEFAULT 1,

    -- ── Timestamps ───────────────────────────────────────────────────────
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_subscriptions_tenant_id             ON subscriptions (tenant_id);
CREATE INDEX idx_subscriptions_stripe_customer_id    ON subscriptions (stripe_customer_id);
CREATE INDEX idx_subscriptions_stripe_subscription_id ON subscriptions (stripe_subscription_id);
CREATE INDEX idx_subscriptions_status                ON subscriptions (status);
CREATE INDEX idx_subscriptions_current_period_end    ON subscriptions (current_period_end)
                                                     WHERE status IN ('active', 'trialing');

COMMENT ON TABLE  subscriptions                      IS 'Mirrors Stripe subscription state per tenant. One active row at a time; history is retained.';
COMMENT ON COLUMN subscriptions.stripe_subscription_id IS 'Stripe sub_... identifier; used for webhook reconciliation.';
COMMENT ON COLUMN subscriptions.monthly_token_limit  IS 'Denormalised from Stripe metadata. NULL = plan has no token cap.';


-- ---------------------------------------------------------------------------
-- 10.  Row-Level Security (RLS) — template policies
-- ---------------------------------------------------------------------------
-- Enable RLS on every tenant-scoped table.
-- Application layer must SET LOCAL app.current_tenant_id = '...' in each
-- transaction so the policies below restrict visibility to that tenant only.
-- ---------------------------------------------------------------------------

ALTER TABLE tenants          ENABLE ROW LEVEL SECURITY;
ALTER TABLE users            ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys         ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_sessions    ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_messages    ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs             ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_logs       ENABLE ROW LEVEL SECURITY;
ALTER TABLE assets           ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscriptions    ENABLE ROW LEVEL SECURITY;

-- Service role bypasses RLS (used by migrations and internal admin tasks).
-- Application role (app_user) is restricted by the policies below.

-- tenants: each tenant can only see its own row
CREATE POLICY tenant_isolation ON tenants
    USING (id = current_setting('app.current_tenant_id', TRUE)::UUID);

-- All other tables: filter by tenant_id column
CREATE POLICY tenant_isolation ON users
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

CREATE POLICY tenant_isolation ON api_keys
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

CREATE POLICY tenant_isolation ON chat_sessions
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

CREATE POLICY tenant_isolation ON chat_messages
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

CREATE POLICY tenant_isolation ON jobs
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

CREATE POLICY tenant_isolation ON usage_logs
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

CREATE POLICY tenant_isolation ON assets
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

CREATE POLICY tenant_isolation ON subscriptions
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);


-- ---------------------------------------------------------------------------
-- 11.  updated_at auto-maintenance trigger
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'tenants','users','api_keys','chat_sessions','chat_messages',
        'jobs','usage_logs','assets','subscriptions'
    ]
    LOOP
        EXECUTE format(
            'CREATE TRIGGER trg_%s_updated_at
             BEFORE UPDATE ON %I
             FOR EACH ROW EXECUTE FUNCTION set_updated_at()',
            t, t
        );
    END LOOP;
END;
$$;


COMMIT;
