-- =====================================================================
-- 0027_add_modality_capture.sql
-- Step 1 (the spine): tag every captured generation with a `modality`
-- (text|image|video|audio) so the moat captures narration, image, AND video
-- signal from one schema — and stop discarding the generating prompt.
--
-- Extends the LIVE capture tables (assets / moat_sessions / correction_pairs);
-- the dead 0017 prompt_events/output_events/revisions tables are left untouched.
-- Idempotent: ADD COLUMN IF NOT EXISTS. Existing table GRANTs cover new columns.
-- =====================================================================

BEGIN;

-- assets — the durable record of every generated image/video/audio file.
-- asset_type is the FILE kind; modality is the GENERATION kind (usually equal,
-- but explicit so one query spans the whole moat). source_prompt was being
-- discarded on every generation — the single highest-value signal to keep.
ALTER TABLE assets
  ADD COLUMN IF NOT EXISTS modality      TEXT
    CHECK (modality IN ('text','image','video','audio')),
  ADD COLUMN IF NOT EXISTS source_prompt TEXT;
CREATE INDEX IF NOT EXISTS idx_assets_modality
  ON assets (tenant_id, modality, created_at DESC) WHERE modality IS NOT NULL;

-- moat_sessions — the narration moat. Tag it 'text' so the whole moat is
-- uniformly filterable by modality alongside the visual assets.
ALTER TABLE moat_sessions
  ADD COLUMN IF NOT EXISTS modality TEXT
    CHECK (modality IN ('text','image','video','audio'));
CREATE INDEX IF NOT EXISTS idx_moat_sessions_modality
  ON moat_sessions (tenant_id, modality) WHERE modality IS NOT NULL;

-- correction_pairs — the edit-signal table (narration today; image/video edits
-- later ride the same column).
ALTER TABLE correction_pairs
  ADD COLUMN IF NOT EXISTS modality TEXT
    CHECK (modality IN ('text','image','video','audio'));
CREATE INDEX IF NOT EXISTS idx_corr_pairs_modality
  ON correction_pairs (tenant_id, modality) WHERE modality IS NOT NULL;

COMMENT ON COLUMN assets.modality           IS 'Generation modality: text|image|video|audio (auto from asset_type). Lets one query span the whole moat.';
COMMENT ON COLUMN assets.source_prompt      IS 'The prompt that produced this asset — captured for the training corpus (was previously discarded).';
COMMENT ON COLUMN moat_sessions.modality    IS 'Generation modality — text for narration sessions.';
COMMENT ON COLUMN correction_pairs.modality IS 'Generation modality of the corrected content — text for narration edits.';

COMMIT;
