-- =====================================================================
-- 0056_add_narasi_job_type.sql
-- SCHEMA-DRIFT FIX — register 'narasi' in job_type_enum.
--
-- Drift: prod's job_type_enum carries 'narasi' (schema_live_raw.sql:62), but NO
-- migration ever ADDs it — 0006 defines the enum without it and nothing ALTERs it
-- in. Prod got the value out-of-band, so a fresh / rebuilt DB (any env restored
-- purely from the migration chain) is MISSING it. That breaks:
--     • database.create_narasi_job / list_narasi_jobs   (cast 'narasi'::job_type_enum → 22P02)
--     • migration 0054's CREATE FUNCTION if it used the enum literal (it dodges this
--       today with `job_type::text = 'narasi'`; this migration removes the need)
-- Hit 3× during Dalang v2 migration validation — this closes it for good.
--
-- Idempotent: IF NOT EXISTS ⟹ a pure no-op on prod (already has 'narasi'); only a
-- fresh chain gains the value. ALTER TYPE ... ADD VALUE is proven-safe through this
-- migrate.js's per-file transaction on Neon (PG15) — see 0053/0020: the value is
-- only ADDED here and never CONSUMED in the same migration (the PG12+ rule), so the
-- transaction wrap is fine.
-- =====================================================================

BEGIN;

ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS 'narasi';

COMMIT;
