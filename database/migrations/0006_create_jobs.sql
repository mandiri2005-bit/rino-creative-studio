-- =====================================================================
-- 0006_create_jobs.sql
-- Enum types (job_type_enum, job_status_enum) + unified jobs table
-- =====================================================================

BEGIN;

-- ── Enum types (idempotent via DO block) ────────────────────────────────────
DO $$ BEGIN
    CREATE TYPE job_type_enum AS ENUM (
        'oneshot_fix',
        'batch_image',
        'tts',
        'imagen',
        'veo',
        'sora'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE job_status_enum AS ENUM (
        'queued',
        'processing',
        'running',
        'cancelling',
        'cancelled',
        'done',
        'error'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ── Table ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS jobs (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID            NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id             UUID            REFERENCES users(id) ON DELETE SET NULL,
    job_type            job_type_enum   NOT NULL,
    status              job_status_enum NOT NULL DEFAULT 'queued',

    -- ── Input configuration ─────────────────────────────────────────────
    model               TEXT,
    input_payload       JSONB,

    -- ── Live progress ───────────────────────────────────────────────────
    progress_current    INTEGER         NOT NULL DEFAULT 0,
    progress_total      INTEGER         NOT NULL DEFAULT 0,
    progress_message    TEXT,
    logs                JSONB           NOT NULL DEFAULT '[]',

    -- ── Output ──────────────────────────────────────────────────────────
    result_payload      JSONB,
    error_message       TEXT,

    -- ── External provider reference ─────────────────────────────────────
    external_job_id     TEXT,
    output_prefix       TEXT,

    -- ── Linkage ─────────────────────────────────────────────────────────
    session_id          UUID            REFERENCES chat_sessions(id) ON DELETE SET NULL,

    -- ── Timestamps ──────────────────────────────────────────────────────
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT now()
);

-- ── Indexes ─────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_jobs_tenant_id       ON jobs (tenant_id);
CREATE INDEX IF NOT EXISTS idx_jobs_user_id         ON jobs (user_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status          ON jobs (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_job_type        ON jobs (tenant_id, job_type);
CREATE INDEX IF NOT EXISTS idx_jobs_external_job_id ON jobs (external_job_id) WHERE external_job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_jobs_created_at      ON jobs (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_session_id      ON jobs (session_id) WHERE session_id IS NOT NULL;

-- ── Comments ────────────────────────────────────────────────────────────────
COMMENT ON TABLE  jobs                  IS 'Unified job table replacing _oneshot_jobs dict, jobs.json, tts-jobs.json, imagen-jobs.json, and the activeJobs Map.';
COMMENT ON COLUMN jobs.input_payload    IS 'Serialised request parameters: prompts[], voice, speed, transcriptBody, model, aspectRatio, etc.';
COMMENT ON COLUMN jobs.result_payload   IS 'Serialised output: fixed_book text, array of {file, url} objects, destFile path, etc.';
COMMENT ON COLUMN jobs.external_job_id  IS 'Provider-assigned identifier: Gemini batch name, Veo task_id, Sora job id.';
COMMENT ON COLUMN jobs.logs             IS 'Ordered array of human-readable log strings mirroring job.logs[] in server.js.';

-- ── Row-Level Security ──────────────────────────────────────────────────────
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON jobs;
CREATE POLICY tenant_isolation ON jobs
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

-- ── updated_at trigger ──────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_jobs_updated_at ON jobs;
CREATE TRIGGER trg_jobs_updated_at
    BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMIT;
