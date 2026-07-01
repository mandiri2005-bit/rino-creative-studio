-- =====================================================================
-- 0051_create_projects.sql
-- "Projects" = user-created collections (folders) that generated assets
-- belong to. A project is the unit the user packages + exports to CapCut /
-- Final Cut / .zip (see 0052 for the assets.project_id link). One flat
-- namespace per tenant; any user in the tenant sees all projects (matches
-- the assets model — tenant-scoped, no per-user ACL).
--
-- Deleting a project is NON-DESTRUCTIVE to its assets: assets.project_id is
-- ON DELETE SET NULL (0052), so the files drop back to "Unassigned" rather
-- than being removed. Membership is single-project-per-asset (a plain FK on
-- assets, not a junction) per the approved design; a many-to-many junction
-- can be layered on later without touching this table.
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS projects (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id     UUID        REFERENCES users(id) ON DELETE SET NULL,  -- creator (informational)
    name        TEXT        NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- recent-first listing per tenant (the Project app's folder grid order).
CREATE INDEX IF NOT EXISTS idx_projects_tenant_recent ON projects (tenant_id, created_at DESC);

-- ── Row-Level Security (codebase standard: ENABLE + FORCE, tenant_isolation) ──
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON projects;
CREATE POLICY tenant_isolation ON projects
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

GRANT SELECT, INSERT, UPDATE, DELETE ON projects TO app_user;

-- ── updated_at trigger ──────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_projects_updated_at ON projects;
CREATE TRIGGER trg_projects_updated_at
    BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE  projects      IS 'User-created collections (folders) that assets belong to; the unit of packaging + export to CapCut/FCP. Tenant-scoped, single-project-per-asset.';
COMMENT ON COLUMN projects.name IS 'User-facing project label, e.g. "INDONESIA STORY". Not unique — the tenant may reuse names.';

COMMIT;
