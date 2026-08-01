-- =====================================================================
-- 0071_narasi_derived_input.sql
--
-- B-07/B-08 Rework 4 (Contract G): registers 'narasi_derived_input' in
-- job_type_enum so Review/One-Shot can write a tenant-scoped derived-
-- operation input record (operation type, source job UUID, validated
-- lineage binding, canonical manuscript language, exact input content
-- hash, and model) BEFORE any provider call. Reuses the existing `jobs`
-- table exactly like a narasi generation job -- input_payload (JSONB)
-- carries the record's fields, `model` uses the existing jobs.model
-- column, and it is completed/failed by its own UUID via the existing
-- finish_narasi_job_by_id (a generic id+tenant-scoped UPDATE that does
-- not filter by job_type). No new table/column required.
--
-- Purely additive: a fresh chain gains the enum value; prod is
-- unaffected until code actually inserts a row with this job_type.
-- =====================================================================

BEGIN;

ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS 'narasi_derived_input';

COMMIT;
