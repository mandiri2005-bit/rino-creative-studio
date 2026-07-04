-- 0058_known_bad_claims_scoping.sql
-- Refactor spec §6: known_bad_claims becomes PER-PROJECT-scopable. "Siege = 93 days"
-- belongs to a Tenochtitlan project and must not fire on a Majapahit manuscript; a
-- small `global` namespace remains for genuinely universal entries. The existing seeds
-- STAY global — their regexes are already context-guarded (require siege/Tenochtitlan
-- in-sentence), so they are safe across projects. Additive; code treats NULL scope as
-- 'global' for rows created before this migration.
BEGIN;

ALTER TABLE narasi_known_bad_claims
  ADD COLUMN IF NOT EXISTS scope      TEXT NOT NULL DEFAULT 'global'
    CHECK (scope IN ('global','project')),
  ADD COLUMN IF NOT EXISTS project_id UUID NULL;

CREATE INDEX IF NOT EXISTS idx_known_bad_claims_project
  ON narasi_known_bad_claims (project_id) WHERE scope = 'project';

COMMENT ON COLUMN narasi_known_bad_claims.scope IS
  'global = fires on every manuscript; project = only when the job''s project_id matches.';

COMMIT;
