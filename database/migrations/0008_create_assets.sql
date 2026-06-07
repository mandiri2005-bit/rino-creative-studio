-- =====================================================================
-- 0008_create_assets.sql
-- Object-storage file references (replaces Docker volume paths)
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS assets (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id                 UUID        REFERENCES users(id) ON DELETE SET NULL,
    job_id                  UUID        REFERENCES jobs(id) ON DELETE SET NULL,

    -- ── Object storage coordinates ────────────────────────────────────────
    bucket                  TEXT        NOT NULL,
    s3_key                  TEXT        NOT NULL,
    original_filename       TEXT,

    -- ── MIME & size ───────────────────────────────────────────────────────
    content_type            TEXT        NOT NULL,
    size_bytes              BIGINT      NOT NULL DEFAULT 0,

    -- ── Asset classification ──────────────────────────────────────────────
    asset_type              TEXT        NOT NULL
                                CHECK (asset_type IN ('video', 'audio', 'image',
                                                      'document', 'archive', 'other')),
    source_job_type         job_type_enum,

    -- ── Pre-signed URL cache ──────────────────────────────────────────────
    signed_url              TEXT,
    signed_url_expires_at   TIMESTAMPTZ,

    -- ── Metadata ─────────────────────────────────────────────────────────
    metadata                JSONB       NOT NULL DEFAULT '{}',
    is_deleted              BOOLEAN     NOT NULL DEFAULT FALSE,

    -- ── Timestamps ───────────────────────────────────────────────────────
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (bucket, s3_key)
);

-- ── Indexes ─────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_assets_tenant_id         ON assets (tenant_id);
CREATE INDEX IF NOT EXISTS idx_assets_job_id            ON assets (job_id) WHERE job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_assets_user_id           ON assets (user_id);
CREATE INDEX IF NOT EXISTS idx_assets_asset_type        ON assets (tenant_id, asset_type);
CREATE INDEX IF NOT EXISTS idx_assets_source_job_type   ON assets (tenant_id, source_job_type);
CREATE INDEX IF NOT EXISTS idx_assets_signed_url_expiry ON assets (signed_url_expires_at)
                                                        WHERE signed_url IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_assets_created_at        ON assets (tenant_id, created_at DESC);

-- ── Comments ────────────────────────────────────────────────────────────────
COMMENT ON TABLE  assets                     IS 'File references in object storage, replacing all Docker volume paths (Veo, Sora, TTS, Imagen, batch).';
COMMENT ON COLUMN assets.s3_key              IS 'Full object key within the bucket, e.g. "tenants/{tid}/jobs/{jid}/output_001.wav".';
COMMENT ON COLUMN assets.signed_url          IS 'Cached pre-signed URL; regenerate when signed_url_expires_at < now().';
COMMENT ON COLUMN assets.metadata            IS 'Free-form JSONB: video duration, image width/height, TTS voice, sample rate, etc.';

-- ── Row-Level Security ──────────────────────────────────────────────────────
ALTER TABLE assets ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON assets;
CREATE POLICY tenant_isolation ON assets
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

-- ── updated_at trigger ──────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_assets_updated_at ON assets;
CREATE TRIGGER trg_assets_updated_at
    BEFORE UPDATE ON assets
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMIT;
