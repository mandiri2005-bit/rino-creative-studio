-- =====================================================================
-- 0050_image_batch_jobs.sql
-- Genuine ASYNC Google batch for the Wimba Image "Batch" tool. Distinct
-- from the synchronous /image/<op> path (image_jobs, 0048): a batch is one
-- Gemini Batch API job (client.batches.create with INLINED requests) that
-- Google fulfils over minutes-to-24h at ~50% of the online price. We bill
-- 50% accordingly (locked 15/25/40 cr for nano-banana / -2 / -pro).
--
-- Native Google ONLY — no aggregators. Auth failover: Vertex OAuth first,
-- then Developer API key (both first-party Google). See batch_engine.py.
--
-- Lifecycle: POST /image/batch/submit holds (price_each × count), inserts a
-- row (status='submitting'), submits to Google, flips to 'processing'. A
-- reconcile loop (+ lazy reconcile on poll) polls each job; on a terminal
-- Google state it persists every produced image to R2/assets, then settles
-- credits: COMMIT for delivered images, REFUND the rest ("yang gagal tidak
-- ditagih"). Settlement is win-gated (finish_image_batch_job flips off
-- 'processing' exactly once) AND idempotent on op_id — no double-charge even
-- if the lazy poll races the loop. Google results expire within ~24h, so a
-- row stuck non-terminal past BATCH_HARD_MAX is refunded + marked 'expired'.
--
-- Results delivery is MANUAL: the row's result_keys feed signed links + a
-- "Download semua (.zip)" button (no auto-download). The assets also land in
-- Media Vault + the Recent rail (assets table = source of truth) regardless.
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS image_batch_jobs (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id         UUID        REFERENCES users(id) ON DELETE SET NULL,
    op_id           TEXT        NOT NULL,            -- credit hold key (commit/refund idempotent on this)
    gemini_job_name TEXT,                            -- Google batch resource name; NULL until submitted
    auth_mode       TEXT,                            -- 'oauth' | 'apikey' — which native-Google path won
    model           TEXT        NOT NULL,            -- catalog id: nano-banana | nano-banana-2 | nano-banana-pro
    vertex_model    TEXT        NOT NULL,            -- resolved Google model id actually submitted
    status          TEXT        NOT NULL DEFAULT 'submitting'
                                CHECK (status IN ('submitting','processing','succeeded','partial','failed','expired')),
    total           INTEGER     NOT NULL DEFAULT 0,  -- # prompts/images requested
    delivered       INTEGER     NOT NULL DEFAULT 0,  -- # images produced+persisted (what we charge for)
    failed          INTEGER     NOT NULL DEFAULT 0,  -- total - delivered (never charged)
    price_each      INTEGER     NOT NULL DEFAULT 0,  -- batch credits per image (15/25/40)
    held_credits    INTEGER     NOT NULL DEFAULT 0,  -- price_each × total reserved at submit
    aspect          TEXT,                            -- aspect ratio applied to every image
    prompts         JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- input prompts (index-aligned with result_keys)
    result_keys     JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- R2 keys aligned to prompts; null entry = that image failed
    error           TEXT,                            -- short failure reason (set on failed/expired)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),   -- IMMUTABLE — the hard-expire anchor (never bumped)
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ                       -- when it reached a terminal status
);

-- recent-first listing per tenant; partial index over the reconcile predicate stays tiny.
CREATE INDEX IF NOT EXISTS idx_ibj_tenant_recent ON image_batch_jobs (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ibj_reconcile     ON image_batch_jobs (updated_at)
    WHERE status IN ('submitting','processing');
CREATE INDEX IF NOT EXISTS idx_ibj_gemini_name   ON image_batch_jobs (gemini_job_name);

-- ── Row-Level Security (codebase standard: ENABLE + FORCE, tenant_isolation) ──
ALTER TABLE image_batch_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE image_batch_jobs FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON image_batch_jobs;
CREATE POLICY tenant_isolation ON image_batch_jobs
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

GRANT SELECT, INSERT, UPDATE, DELETE ON image_batch_jobs TO app_user;

-- ── updated_at trigger ──────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_image_batch_jobs_updated_at ON image_batch_jobs;
CREATE TRIGGER trg_image_batch_jobs_updated_at
    BEFORE UPDATE ON image_batch_jobs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── Cross-tenant "due for reconcile" scan ────────────────────────────────────
-- The reconcile loop must find still-running batches across EVERY tenant (the
-- user may never poll), which a NOBYPASSRLS role can't do under tenant_isolation
-- — so SECURITY DEFINER (same precedent as 0048's sweep). It only READS ids; the
-- loop then re-reads each full row tenant-scoped via get_image_batch_job() and
-- does all writes (commit/refund/finish) under the owning tenant. p_max_age skips
-- rows touched within the window so a 30s loop doesn't re-poll a just-updated job.
CREATE OR REPLACE FUNCTION image_batch_jobs_due(p_max_age interval)
RETURNS TABLE (id UUID, tenant_id UUID)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT image_batch_jobs.id, image_batch_jobs.tenant_id
      FROM image_batch_jobs
     WHERE image_batch_jobs.status IN ('submitting','processing')
       AND image_batch_jobs.updated_at < now() - p_max_age;
$$;

REVOKE ALL    ON FUNCTION image_batch_jobs_due(interval) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION image_batch_jobs_due(interval) TO app_user;

COMMENT ON TABLE  image_batch_jobs              IS 'Async Google Batch API jobs for the Image Batch tool (50%-price native-Google batch). One row per submit.';
COMMENT ON COLUMN image_batch_jobs.op_id        IS 'Credit hold key — metering.commit_credits/refund_credits are idempotent on it.';
COMMENT ON COLUMN image_batch_jobs.created_at   IS 'IMMUTABLE submit time; hard-expire compares against THIS (updated_at is bumped by polling).';
COMMENT ON COLUMN image_batch_jobs.result_keys  IS 'R2 object keys index-aligned with prompts (null = that image failed); signed on read for the download links.';

COMMIT;
