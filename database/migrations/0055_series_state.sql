-- =====================================================================
-- 0055_series_state.sql  (Dalang v2 Slice 3 — Series State; owns the schema
-- that Persona Studio / Showrunner will reuse, per spec §C D1).
-- Three tenant-scoped tables backing multi-episode/chapter continuity:
--   series          — one row per series (kind book|channel + free-form spec).
--   series_facts    — canonical entity/continuity facts (fact ledger, §3.1).
--   series_summary  — one rolling "story so far" per series (1:1, §3.2).
-- For Dalang a series.id == the narasi jobs.id (a book job); Showrunner will
-- create kind='channel' rows. Conventions mirror 0001-0052: UUID PK,
-- tenant_id FK ON DELETE CASCADE, set_updated_at() trigger, ENABLE+FORCE RLS
-- with the canonical tenant_isolation policy, explicit app_user grants.
-- Additive: nothing reads/writes these until DALANG_V2_ENABLED /
-- DALANG_FACT_LEDGER_ENABLED turn the v2 loop on.
-- =====================================================================

BEGIN;

-- ── 1. series ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS series (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id     UUID        REFERENCES users(id) ON DELETE SET NULL,
    kind        TEXT        NOT NULL CHECK (kind IN ('book','channel')),
    spec        JSONB       NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── 2. series_facts (fact ledger) ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS series_facts (
    id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    series_id          UUID        NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    tenant_id          UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    entity_key         TEXT        NOT NULL,
    entity_type        TEXT        NOT NULL,
    value              JSONB       NOT NULL DEFAULT '{}',
    introduced_episode INTEGER,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (series_id, entity_key)          -- one canonical row per entity → enables upsert
);

-- ── 3. series_summary (rolling "story so far", 1 row per series) ─────────────
CREATE TABLE IF NOT EXISTS series_summary (
    series_id       UUID        PRIMARY KEY REFERENCES series(id) ON DELETE CASCADE,
    tenant_id       UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    rolling_summary TEXT        NOT NULL DEFAULT '',
    updated_episode INTEGER,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Indexes (idx_<table>_<cols>, tenant-first) ──────────────────────────────
CREATE INDEX IF NOT EXISTS idx_series_tenant_recent  ON series (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_series_facts_series   ON series_facts (series_id, entity_key);
CREATE INDEX IF NOT EXISTS idx_series_facts_tenant   ON series_facts (tenant_id);
CREATE INDEX IF NOT EXISTS idx_series_summary_tenant ON series_summary (tenant_id);

-- ── RLS: ENABLE + FORCE + canonical tenant_isolation (USING + WITH CHECK) ────
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['series','series_facts','series_summary'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY;', t);
    EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY;', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I;', t);
    EXECUTE format($f$
      CREATE POLICY tenant_isolation ON %I
        USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);
    $f$, t);
  END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON series, series_facts, series_summary TO app_user;

-- ── updated_at triggers (only tables WITH updated_at) ───────────────────────
DROP TRIGGER IF EXISTS trg_series_facts_updated_at ON series_facts;
CREATE TRIGGER trg_series_facts_updated_at
    BEFORE UPDATE ON series_facts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_series_summary_updated_at ON series_summary;
CREATE TRIGGER trg_series_summary_updated_at
    BEFORE UPDATE ON series_summary
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE series         IS 'Dalang v2 Series State: one row per series (Dalang book = narasi jobs.id; Showrunner channel). kind book|channel; spec free-form.';
COMMENT ON TABLE series_facts   IS 'Fact ledger (§3.1): canonical entity/continuity facts introduced across a series, upserted on (series_id, entity_key).';
COMMENT ON TABLE series_summary IS 'Rolling per-series summary (§3.2): compressed story-so-far, 1:1 with series, upserted per episode.';

COMMIT;
