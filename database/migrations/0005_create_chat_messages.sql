-- =====================================================================
-- 0005_create_chat_messages.sql
-- Individual message turns inside chat sessions
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS chat_messages (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    session_id          UUID        NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role                TEXT        NOT NULL
                            CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content             TEXT        NOT NULL DEFAULT '',
    tool_calls          JSONB,
    tool_results        JSONB,
    finish_reason       TEXT,
    tokens_in           INTEGER,
    tokens_out          INTEGER,
    sequence_number     INTEGER     NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Indexes ─────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_chat_messages_session_id ON chat_messages (session_id, sequence_number);
CREATE INDEX IF NOT EXISTS idx_chat_messages_tenant_id  ON chat_messages (tenant_id);
CREATE INDEX IF NOT EXISTS idx_chat_messages_created_at ON chat_messages (session_id, created_at);

-- ── Comments ────────────────────────────────────────────────────────────────
COMMENT ON TABLE  chat_messages                 IS 'Individual message turns inside a chat session. Replaces history arrays in Conversation objects.';
COMMENT ON COLUMN chat_messages.tool_calls      IS 'JSONB array of tool_use blocks emitted by the model (MCP).';
COMMENT ON COLUMN chat_messages.sequence_number IS '1-based monotonic counter within the session for stable ordering.';

-- ── Row-Level Security ──────────────────────────────────────────────────────
ALTER TABLE chat_messages ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON chat_messages;
CREATE POLICY tenant_isolation ON chat_messages
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

-- ── updated_at trigger ──────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_chat_messages_updated_at ON chat_messages;
CREATE TRIGGER trg_chat_messages_updated_at
    BEFORE UPDATE ON chat_messages
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMIT;
