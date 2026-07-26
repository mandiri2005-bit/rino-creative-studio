-- =====================================================================
-- 0070_narasi_lifecycle.sql
--
-- B-07/B-08: additive, non-destructive lifecycle metadata for immutable
-- target-language resolution and stable chapter identity. NARASI_LIFECYCLE_V1
-- stays default OFF; every column here is nullable and every existing row
-- (and every flag-off code path) is completely unaffected.
--
--   narasi_outlines.lifecycle_schema_version / lifecycle_hash / invalidated_at
--       — the admitted outline_lifecycle envelope's schema version + canonical
--         hash, and when set, the moment a real target_language change
--         invalidated this outline's dependent artifacts.
--   narasi_chapters.chapter_id
--       — the stable, language-neutral ch_[0-9a-f]{32} identity minted at
--         outline admission. NULL for a legacy/pre-v1 chapter row.
--
-- v1 upsert identity is (job_id, chapter_id), enforced by a PARTIAL unique
-- index (both non-null) so it coexists with the existing full
-- (job_id, chapter_index) unique constraint from 0017 without conflicting —
-- chapter_index remains for ordering and legacy compatibility only, never
-- upsert identity, once a row carries a chapter_id.
-- =====================================================================

BEGIN;

-- ── narasi_outlines: lifecycle metadata (nullable — legacy rows unaffected) ──
-- Rework 2 (Contract C): outline_lifecycle JSONB stores the COMPLETE validated envelope
-- (schema_version/outline_id/target_language/status/chapters/bindings/lifecycle_hash) so a
-- v1 outline round-trips byte-for-byte, not just its schema_version/hash metadata. The two
-- existing metadata columns stay for indexed lookups without decoding the JSONB blob.
ALTER TABLE narasi_outlines
    ADD COLUMN IF NOT EXISTS lifecycle_schema_version TEXT,
    ADD COLUMN IF NOT EXISTS lifecycle_hash            TEXT,
    ADD COLUMN IF NOT EXISTS invalidated_at             TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS outline_lifecycle          JSONB;

DO $$
BEGIN
    ALTER TABLE narasi_outlines
        ADD CONSTRAINT narasi_outlines_lifecycle_hash_format
        CHECK (lifecycle_hash IS NULL OR lifecycle_hash ~ '^[0-9a-f]{64}$');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ── narasi_chapters: nullable stable chapter_id (v1 identity) ──────────────
ALTER TABLE narasi_chapters
    ADD COLUMN IF NOT EXISTS chapter_id TEXT;

DO $$
BEGIN
    ALTER TABLE narasi_chapters
        ADD CONSTRAINT narasi_chapters_chapter_id_format
        CHECK (chapter_id IS NULL OR chapter_id ~ '^ch_[0-9a-f]{32}$');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Rework 2 (Contract C): the original 0017 (job_id, chapter_index) constraint was
-- UNCONDITIONAL -- it still fires on every row regardless of chapter_id, so a v1 two-
-- chapter reorder (row A moving to row B's old index while both carry real chapter_ids)
-- could collide on it even though the (job_id, chapter_id) partial index below would
-- never care. Drop it and replace with two partial indexes that never overlap: legacy
-- rows (chapter_id IS NULL) keep exactly the old uniqueness; v1 rows (chapter_id IS NOT
-- NULL) are governed ONLY by (job_id, chapter_id), so chapter_index can move freely.
DO $$
BEGIN
    ALTER TABLE narasi_chapters DROP CONSTRAINT narasi_chapters_job_id_chapter_index_key;
EXCEPTION WHEN undefined_object THEN NULL;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_narasi_chapters_job_chapter_index_legacy
    ON narasi_chapters (job_id, chapter_index)
    WHERE chapter_id IS NULL;

-- Partial unique index: v1 identity is (job_id, chapter_id). NULLs are
-- distinct, so this has zero effect until a row actually carries a non-null
-- chapter_id.
CREATE UNIQUE INDEX IF NOT EXISTS idx_narasi_chapters_job_chapter_id
    ON narasi_chapters (job_id, chapter_id)
    WHERE job_id IS NOT NULL AND chapter_id IS NOT NULL;

-- ── Derived lineage (Contract F): Review/One-Shot/save-edit persist the source job's
-- durable UUID plus their exact validated binding, not just a transient response field. ──
ALTER TABLE moat_sessions
    ADD COLUMN IF NOT EXISTS source_job_uuid UUID REFERENCES jobs(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS lineage_binding  JSONB;

ALTER TABLE correction_pairs
    ADD COLUMN IF NOT EXISTS source_job_uuid UUID REFERENCES jobs(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS lineage_binding  JSONB;

COMMENT ON COLUMN narasi_outlines.lifecycle_schema_version IS 'B-07/B-08: continuity.lifecycle LIFECYCLE_SCHEMA_VERSION for this outline, or NULL for a legacy/unbound outline.';
COMMENT ON COLUMN narasi_outlines.lifecycle_hash            IS 'B-07/B-08: canonical outline_lifecycle lifecycle_hash (lowercase sha256), or NULL for a legacy/unbound outline.';
COMMENT ON COLUMN narasi_outlines.invalidated_at            IS 'B-07/B-08: set when a real target_language change invalidated this outline''s dependent artifacts (outline/brief/story_contract/chapter_plan).';
COMMENT ON COLUMN narasi_outlines.outline_lifecycle         IS 'B-07/B-08 Rework 2: the complete validated outline_lifecycle envelope, round-tripped byte-for-byte; NULL for a legacy/unbound outline.';
COMMENT ON COLUMN narasi_chapters.chapter_id                IS 'B-07/B-08: stable, language-neutral chapter identity (ch_[0-9a-f]{32}), or NULL for a legacy/unbound chapter. Never mutated by rename/reorder.';
COMMENT ON COLUMN moat_sessions.source_job_uuid             IS 'B-07/B-08 Rework 2: the source job this derived call (Review/One-Shot) is bound to, or NULL for a standalone/legacy call.';
COMMENT ON COLUMN moat_sessions.lineage_binding             IS 'B-07/B-08 Rework 2: the exact validate_lifecycle_artifact() binding proving this call is bound to source_job_uuid''s lifecycle state.';
COMMENT ON COLUMN correction_pairs.source_job_uuid          IS 'B-07/B-08 Rework 2: the source job this saved edit is bound to, or NULL for a standalone/legacy edit.';
COMMENT ON COLUMN correction_pairs.lineage_binding          IS 'B-07/B-08 Rework 2: the exact validate_lifecycle_artifact() binding proving this edit is bound to source_job_uuid''s lifecycle state.';

COMMIT;
