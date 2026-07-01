-- =====================================================================
-- 0052_add_project_id_to_assets.sql
-- Link generated assets to a project (0051). Single-project-per-asset:
-- one nullable FK, not a junction table.
--
-- ON DELETE SET NULL: deleting a project is non-destructive — its assets
-- drop back to "Unassigned" (project_id IS NULL) rather than being removed.
-- project_id NULL is the default and the "Unassigned" pseudo-folder in the
-- Project app. Assets created before this migration (or by generators that
-- don't yet send a project) simply stay NULL until moved.
-- =====================================================================

BEGIN;

ALTER TABLE assets
    ADD COLUMN IF NOT EXISTS project_id UUID REFERENCES projects(id) ON DELETE SET NULL;

-- Fast "assets in this project, recent-first" lookups (the folder-open view).
-- Partial: only the assigned rows are indexed — Unassigned scans stay on the
-- existing tenant/created_at index and this stays small.
CREATE INDEX IF NOT EXISTS idx_assets_project_id
    ON assets (tenant_id, project_id, created_at DESC)
    WHERE project_id IS NOT NULL;

COMMENT ON COLUMN assets.project_id IS 'Owning project (0051). NULL = Unassigned. ON DELETE SET NULL keeps assets when a project is deleted.';

COMMIT;
