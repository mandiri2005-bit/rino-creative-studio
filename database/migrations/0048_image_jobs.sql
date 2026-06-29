-- =====================================================================
-- 0048_image_jobs.sql
-- Async submit+poll for the Wimba Image page. A slow provider (e.g.
-- seedream-5 ~125s) was being killed by the synchronous long-poll: the
-- client held one HTTP connection open for the whole generation and the
-- 120s dispatch deadline fired before the provider finished → false
-- "Couldn't generate" while the image (and its COGS) succeeded upstream.
--
-- New model: POST /image/<op>/submit holds credits, inserts an image_jobs
-- row (status='running'), spawns a background task that runs the SAME
-- dispatch→persist→commit path, and returns {job_id} in ~1s. The client
-- polls GET /image/jobs/<id> every 10s. The job completes server-side even
-- if the user navigates away — it lands in assets/Media Vault + the Recent
-- rail regardless (see 0008 assets, /api/assets).
--
-- Credit safety: the hold is taken at submit; the background task commits on
-- confirmed output or refunds on ANY failure (op_id-keyed, idempotent —
-- mirrors the sync image_op). A process restart mid-job orphans the hold;
-- image_jobs_sweep_stale() (run at startup + lazily on poll) marks such jobs
-- failed and hands their (tenant_id, op_id) back to the app to refund.
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS image_jobs (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id     UUID        REFERENCES users(id) ON DELETE SET NULL,
    op_id       TEXT        NOT NULL,                 -- credit hold key (commit/refund are idempotent on this)
    op          TEXT        NOT NULL,                 -- URL op: create|edit|reframe|upscale|vectorize|bg_remove…
    feature     TEXT        NOT NULL,                 -- resolved registry feature
    model       TEXT        NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'running'
                            CHECK (status IN ('running','success','failed')),
    credits     INTEGER     NOT NULL DEFAULT 0,       -- credits actually charged (set on success)
    result_key  TEXT,                                 -- R2 object key OR data: URI of the output (signed on read)
    result_mime TEXT,
    error       TEXT,                                 -- short failure reason (set on failure)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- recent-first listing per tenant; partial index over the sweep predicate keeps it tiny.
CREATE INDEX IF NOT EXISTS idx_image_jobs_tenant ON image_jobs (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_image_jobs_running ON image_jobs (updated_at) WHERE status = 'running';

-- ── Row-Level Security (codebase standard: ENABLE + FORCE, tenant_isolation) ──
ALTER TABLE image_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE image_jobs FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON image_jobs;
CREATE POLICY tenant_isolation ON image_jobs
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

GRANT SELECT, INSERT, UPDATE, DELETE ON image_jobs TO app_user;

-- ── updated_at trigger ──────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_image_jobs_updated_at ON image_jobs;
CREATE TRIGGER trg_image_jobs_updated_at
    BEFORE UPDATE ON image_jobs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── Cross-tenant orphan sweep ────────────────────────────────────────────────
-- A process restart kills in-flight background tasks, leaving jobs 'running'
-- with a held (un-committed, un-refunded) credit reservation. This is
-- inherently cross-tenant (one query over every tenant's stale rows), which a
-- NOBYPASSRLS role can't do under the tenant_isolation policy — so SECURITY
-- DEFINER (same precedent as the 0046 Clerk identity helpers). It atomically
-- marks stale rows failed and RETURNS their (tenant_id, op_id) so the app can
-- refund each hold (metering.refund_credits is idempotent → safe to re-run).
CREATE OR REPLACE FUNCTION image_jobs_sweep_stale(p_older_than interval)
RETURNS TABLE (tenant_id UUID, op_id TEXT)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    UPDATE image_jobs
       SET status = 'failed', error = 'process restarted', updated_at = now()
     WHERE status = 'running'
       AND updated_at < now() - p_older_than
    RETURNING image_jobs.tenant_id, image_jobs.op_id;
$$;

GRANT EXECUTE ON FUNCTION image_jobs_sweep_stale(interval) TO app_user;

COMMENT ON TABLE  image_jobs            IS 'Async submit+poll jobs for the Image page. One row per /image/<op>/submit; background task fills result_key + status.';
COMMENT ON COLUMN image_jobs.op_id      IS 'Credit hold key — commit_credits/refund_credits are idempotent on it.';
COMMENT ON COLUMN image_jobs.result_key IS 'R2 object key (signed on read) or a data: URI; what GET /image/jobs/<id> resolves to image_url.';

COMMIT;
