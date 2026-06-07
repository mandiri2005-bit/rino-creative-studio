-- =====================================================================
-- 0010_create_migrations_table.sql
-- Tracks which migration files have been applied
-- (Also bootstrapped by migrate.js on first run)
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS migrations (
    id              SERIAL      PRIMARY KEY,
    filename        TEXT        NOT NULL UNIQUE,
    checksum        TEXT,                            -- MD5 of file contents at apply time
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_ms     INTEGER                          -- how long the migration took
);

CREATE INDEX IF NOT EXISTS idx_migrations_filename ON migrations (filename);

COMMENT ON TABLE  migrations          IS 'Records every migration file that has been applied. Used by migrate.js to skip already-run files.';
COMMENT ON COLUMN migrations.checksum IS 'MD5 hash of the .sql file contents at the time it was applied, for drift detection.';

COMMIT;
