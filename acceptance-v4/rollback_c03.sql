\set ON_ERROR_STOP on

BEGIN;

DO $$
DECLARE
  table_name text;
  row_exists boolean;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'narasi_continuity_recovery_artifacts',
    'narasi_continuity_coverage',
    'narasi_continuity_violations',
    'narasi_continuity_claims',
    'narasi_continuity_contracts'
  ] LOOP
    IF to_regclass('public.' || table_name) IS NOT NULL THEN
      EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I LIMIT 1)', table_name) INTO row_exists;
      IF row_exists THEN
        RAISE EXCEPTION 'C-03 rollback refused: %. contains retained rows', table_name;
      END IF;
    END IF;
  END LOOP;
END $$;

DROP TABLE IF EXISTS narasi_continuity_recovery_artifacts;
DROP TABLE IF EXISTS narasi_continuity_coverage;
DROP TABLE IF EXISTS narasi_continuity_violations;
DROP TABLE IF EXISTS narasi_continuity_claims;
DROP TABLE IF EXISTS narasi_continuity_contracts;

DROP FUNCTION IF EXISTS narasi_c03_guard_recovery_update();
DROP FUNCTION IF EXISTS narasi_c03_guard_coverage_update();
DROP FUNCTION IF EXISTS narasi_c03_guard_violation_update();
DROP FUNCTION IF EXISTS narasi_c03_guard_contract_update();
DROP FUNCTION IF EXISTS narasi_c03_guard_contract_insert();
DROP FUNCTION IF EXISTS narasi_c03_reject_update();
DROP FUNCTION IF EXISTS narasi_c03_valid_access_audit(jsonb);
DROP FUNCTION IF EXISTS narasi_c03_valid_coverage(jsonb);
DROP FUNCTION IF EXISTS narasi_c03_valid_evidence(jsonb);
DROP FUNCTION IF EXISTS narasi_c03_valid_amendment(integer, uuid, text, jsonb, jsonb, jsonb);
DROP FUNCTION IF EXISTS narasi_c03_valid_slice_hashes(jsonb, jsonb);
DROP FUNCTION IF EXISTS narasi_c03_valid_chapter_ids(jsonb, boolean);

DROP INDEX IF EXISTS uq_c03_projects_tenant_id_id;
DROP INDEX IF EXISTS uq_c03_jobs_tenant_id_id;

COMMIT;
