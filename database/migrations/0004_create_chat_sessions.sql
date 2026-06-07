-- =====================================================================
-- 0004_create_chat_sessions.sql
-- Persistent chat sessions (replaces in-memory sessions dict)
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS chat_sessions (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id             UUID        REFERENCES users(id) ON DELETE SET NULL,
    title               TEXT,
    model               TEXT        NOT NULL,
    system_prompt       TEXT        NOT NULL DEFAULT '',
    temperature         NUMERIC(4,3) NOT NULL DEFAULT 0.9
                            CHECK (temperature BETWEEN 0 AND 2),
    max_tokens          INTEGER     NOT NULL DEFAULT 8192,
    use_tools           BOOLEAN     NOT NULL DEFAULT FALSE,
    mcp_paths           TEXT,
    is_archived         BOOLEAN     NOT NULL DEFAULT FALSE,
    last_message_at     TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Indexes ─────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_chat_sessions_tenant_id       ON chat_sessions (tenant_id);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_id         ON chat_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_last_message_at ON chat_sessions (tenant_id, last_message_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_is_archived     ON chat_sessions (tenant_id, is_archived);

-- ── Comments ────────────────────────────────────────────────────────────────
COMMENT ON TABLE  chat_sessions             IS 'Persistent chat sessions. Replaces the in-memory sessions dict in laozhang_api.py.';
COMMENT ON COLUMN chat_sessions.mcp_paths   IS 'Comma-separated folder paths forwarded to the MCP file-search sidecar.';
COMMENT ON COLUMN chat_sessions.last_message_at IS 'Denormalised timestamp of the most recent message for efficient sorting.';

-- ── Row-Level Security ──────────────────────────────────────────────────────
ALTER TABLE chat_sessions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON chat_sessions;
CREATE POLICY tenant_isolation ON chat_sessions
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

-- ── updated_at trigger ──────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_chat_sessions_updated_at ON chat_sessions;
CREATE TRIGGER trg_chat_sessions_updated_at
    BEFORE UPDATE ON chat_sessions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMIT;
