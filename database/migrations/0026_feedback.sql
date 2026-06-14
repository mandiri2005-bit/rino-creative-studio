-- =====================================================================
-- 0026_feedback.sql
-- In-app user feedback. One row per submission, tenant-scoped (RLS forced to
-- the codebase standard). email optional (captured from the logged-in user when
-- left blank); user_name denormalised for quick reading.
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS feedback (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id     UUID        REFERENCES users(id) ON DELETE SET NULL,
    user_name   TEXT,
    email       TEXT,
    body        TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_feedback_tenant ON feedback (tenant_id, created_at DESC);

ALTER TABLE feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE feedback FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON feedback;
CREATE POLICY tenant_isolation ON feedback
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

GRANT SELECT, INSERT, UPDATE, DELETE ON feedback TO app_user;

COMMIT;
