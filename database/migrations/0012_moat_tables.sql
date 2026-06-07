-- 0012_moat_tables.sql
-- WS-G Task 5: moat capture tables. Stores every generated narration and every
-- correction (original AI text → human/optimized text) as training data.
-- Column types mirror schema.sql conventions (UUID PK, timestamptz, numeric).

BEGIN;

CREATE TABLE IF NOT EXISTS moat_sessions (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  user_id              UUID REFERENCES users(id) ON DELETE SET NULL,
  topic                TEXT,
  style                TEXT,
  rag_used             BOOLEAN DEFAULT FALSE,
  sources              JSONB,
  passages             JSONB,
  prompt_used          TEXT,
  generated_narration  TEXT,
  model                TEXT,
  tokens_in            INTEGER DEFAULT 0,
  tokens_out           INTEGER DEFAULT 0,
  cost_usd             NUMERIC(12,8) DEFAULT 0,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS correction_pairs (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  moat_session_id   UUID REFERENCES moat_sessions(id) ON DELETE CASCADE,
  tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  user_id           UUID REFERENCES users(id) ON DELETE SET NULL,
  original_text     TEXT,
  corrected_text    TEXT,
  edit_distance     INTEGER,
  edit_ratio        NUMERIC(6,4),
  quality_tier      TEXT,                  -- 'high' | 'low' | 'rewrite'
  style_label       TEXT,
  topic             TEXT,
  duration_minutes  INTEGER,
  language          TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_moat_sessions_tenant  ON moat_sessions (tenant_id);
CREATE INDEX IF NOT EXISTS idx_moat_sessions_created ON moat_sessions (created_at);
CREATE INDEX IF NOT EXISTS idx_corr_tenant           ON correction_pairs (tenant_id);
CREATE INDEX IF NOT EXISTS idx_corr_quality          ON correction_pairs (quality_tier);
CREATE INDEX IF NOT EXISTS idx_corr_style            ON correction_pairs (style_label);
CREATE INDEX IF NOT EXISTS idx_corr_created          ON correction_pairs (created_at);

COMMIT;
