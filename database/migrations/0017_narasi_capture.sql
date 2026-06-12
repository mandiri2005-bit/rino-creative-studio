-- =====================================================================
-- 0017_narasi_capture.sql
-- Step 1 (durable + captured output): the operational read-back store for
-- generated narration, plus four event/signal tables that seed the moat.
--
--   narasi_chapters  — durable per-chapter text (read job back from DB, never
--                      regenerate) + capture columns (source_prompt,
--                      retrieved_ids, edit_distance, approved).
--   prompt_events    — one row per generation prompt (analytics, like usage_logs).
--   output_events    — one row per generation output (analytics).
--   revisions        — every human edit (before/after + edit_distance).
--   approvals        — 1-5 rating / approval signal per chapter.
--
-- Conventions mirror 0001-0016: UUID PK gen_random_uuid(), tenant_id FK
-- ON DELETE CASCADE, timestamptz, set_updated_at() trigger, FORCE-RLS with the
-- tenant_iso policy from 0013, explicit app_user grants from 0016.
--
-- DELIBERATE DECISION — job_id is ON DELETE SET NULL, NOT CASCADE:
--   cleanup_old_jobs() (database.py) DELETEs done/error jobs after 24h. With
--   CASCADE, every chapter + its retrieved_ids would be wiped 24h after the job
--   finishes — defeating the entire point of this step. SET NULL keeps the
--   captured rows alive after the operational job is purged.
--   (To also keep a job *reopenable* long-term, cleanup_old_jobs must be taught
--   to skip jobs that have narasi_chapters — that is a database.py change in the
--   write/read steps, not this migration.)
-- =====================================================================

BEGIN;

-- ── 1. narasi_chapters — durable operational store + capture ────────────────
CREATE TABLE IF NOT EXISTS narasi_chapters (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id         UUID        REFERENCES users(id) ON DELETE SET NULL,
    job_id          UUID        REFERENCES jobs(id)  ON DELETE SET NULL,  -- SET NULL: survive cleanup_old_jobs()
    chapter_index   INTEGER     NOT NULL,
    content         TEXT        NOT NULL DEFAULT '',
    word_count      INTEGER     NOT NULL DEFAULT 0,
    version         INTEGER     NOT NULL DEFAULT 1,
    source_prompt   TEXT,
    retrieved_ids   JSONB       NOT NULL DEFAULT '[]',
    edit_distance   INTEGER     NOT NULL DEFAULT 0,
    approved        BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- natural key while the job exists → enables idempotent upsert on retry
    -- (NULLs are distinct, so this stops applying once job_id is nulled out)
    UNIQUE (job_id, chapter_index)
);

-- ── 2. prompt_events — per-generation prompt (analytics) ────────────────────
CREATE TABLE IF NOT EXISTS prompt_events (
    id          UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID          NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id     UUID          REFERENCES users(id) ON DELETE SET NULL,
    job_id      UUID          REFERENCES jobs(id)  ON DELETE SET NULL,
    model       TEXT,
    prompt      TEXT,
    style       TEXT,
    lang        TEXT,
    tokens      INTEGER       NOT NULL DEFAULT 0,
    cost_usd    NUMERIC(12,8) NOT NULL DEFAULT 0,   -- named cost_usd to match usage_logs
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- ── 3. output_events — per-generation output (analytics) ────────────────────
CREATE TABLE IF NOT EXISTS output_events (
    id          UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID          NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id     UUID          REFERENCES users(id) ON DELETE SET NULL,
    job_id      UUID          REFERENCES jobs(id)  ON DELETE SET NULL,
    model       TEXT,
    output      TEXT,
    tokens      INTEGER       NOT NULL DEFAULT 0,
    cost_usd    NUMERIC(12,8) NOT NULL DEFAULT 0,
    rag_used    BOOLEAN       NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- ── 4. revisions — every human edit (correction signal) ─────────────────────
CREATE TABLE IF NOT EXISTS revisions (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id       UUID        REFERENCES users(id) ON DELETE SET NULL,
    chapter_id    UUID        NOT NULL REFERENCES narasi_chapters(id) ON DELETE CASCADE,
    before_text   TEXT,
    after_text    TEXT,
    edit_distance INTEGER,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── 5. approvals — 1-5 rating / approval signal ─────────────────────────────
CREATE TABLE IF NOT EXISTS approvals (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id     UUID        REFERENCES users(id) ON DELETE SET NULL,
    chapter_id  UUID        NOT NULL REFERENCES narasi_chapters(id) ON DELETE CASCADE,
    approved    BOOLEAN     NOT NULL DEFAULT FALSE,
    rating      SMALLINT    CHECK (rating BETWEEN 1 AND 5),   -- nullable: rating optional
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Indexes (tenant_id + job_id/chapter_id, per spec) ───────────────────────
CREATE INDEX IF NOT EXISTS idx_narasi_chapters_tenant_id ON narasi_chapters (tenant_id);
CREATE INDEX IF NOT EXISTS idx_narasi_chapters_job_id    ON narasi_chapters (job_id) WHERE job_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_prompt_events_tenant_id   ON prompt_events (tenant_id);
CREATE INDEX IF NOT EXISTS idx_prompt_events_job_id      ON prompt_events (job_id) WHERE job_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_output_events_tenant_id   ON output_events (tenant_id);
CREATE INDEX IF NOT EXISTS idx_output_events_job_id      ON output_events (job_id) WHERE job_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_revisions_tenant_id       ON revisions (tenant_id);
CREATE INDEX IF NOT EXISTS idx_revisions_chapter_id      ON revisions (chapter_id);

CREATE INDEX IF NOT EXISTS idx_approvals_tenant_id       ON approvals (tenant_id);
CREATE INDEX IF NOT EXISTS idx_approvals_chapter_id      ON approvals (chapter_id);

-- ── updated_at trigger (only narasi_chapters has updated_at) ─────────────────
DROP TRIGGER IF EXISTS trg_narasi_chapters_updated_at ON narasi_chapters;
CREATE TRIGGER trg_narasi_chapters_updated_at
    BEFORE UPDATE ON narasi_chapters
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── Row-Level Security: ENABLE + FORCE + tenant_iso (mirrors 0013) ──────────
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'narasi_chapters','prompt_events','output_events','revisions','approvals'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY;', t);
    EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY;', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_iso ON %I;', t);
    EXECUTE format($f$
      CREATE POLICY tenant_iso ON %I
        USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);
    $f$, t);
  END LOOP;
END $$;

-- ── Explicit grants to app_user (non-BYPASSRLS). 0016's ALTER DEFAULT
--    PRIVILEGES already auto-grants owner-created tables, but be explicit.
--    All PKs are UUID → no sequences to grant. ──────────────────────────────
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON
      narasi_chapters, prompt_events, output_events, revisions, approvals
      TO app_user;
  END IF;
END $$;

-- ── Comments ────────────────────────────────────────────────────────────────
COMMENT ON TABLE narasi_chapters IS 'Durable per-chapter narration (operational read-back) + capture columns (source_prompt, retrieved_ids, edit_distance, approved). job_id is SET NULL so rows survive cleanup_old_jobs().';
COMMENT ON TABLE prompt_events   IS 'One row per generation prompt (model/style/lang/tokens/cost). Analytics sibling of usage_logs.';
COMMENT ON TABLE output_events   IS 'One row per generation output (model/tokens/cost/rag_used).';
COMMENT ON TABLE revisions       IS 'Every human edit of a chapter (before/after + edit_distance). Correction signal for the moat.';
COMMENT ON TABLE approvals       IS '1-5 rating / approval signal per chapter. approved=true on rating >= 4 (set by app layer).';

COMMENT ON COLUMN narasi_chapters.retrieved_ids IS 'JSONB array of passage_id strings retrieved for this chapter (RAG provenance). Auto-encoded by the asyncpg jsonb codec in database.py.';
COMMENT ON COLUMN narasi_chapters.job_id        IS 'ON DELETE SET NULL — NOT cascade — so captured rows outlive cleanup_old_jobs() (24h job purge).';

COMMIT;
