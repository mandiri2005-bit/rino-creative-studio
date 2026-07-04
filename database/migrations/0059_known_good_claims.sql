-- 0059_known_good_claims.sql
-- R-FG8 §4: verified-claim cache, mirror of known_bad_claims. Every (future) verification
-- result writes an entry; matched claims are exempt from the fact-scan sweep (noise
-- damping now, near-zero re-verification cost when the search pass lands). Also the
-- audit artifact: the manuscript's evidentiary trail. Per-project scoped with a global
-- namespace, same contract as 0058. No automatic invalidation (historical facts don't
-- move fast); a manual purge that overturns an entry should also write the known_bad row.
BEGIN;

CREATE TABLE IF NOT EXISTS narasi_known_good_claims (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  claim_pattern   TEXT NOT NULL,          -- python regex; matched sentences are sweep-exempt
  verified_value  TEXT NOT NULL DEFAULT '',
  epistemic_class TEXT NOT NULL DEFAULT 'world'
    CHECK (epistemic_class IN ('world','source','scholarly','attribution')),
  source          TEXT NOT NULL DEFAULT '',
  scope           TEXT NOT NULL DEFAULT 'global' CHECK (scope IN ('global','project')),
  project_id      UUID NULL,
  verified_date   TIMESTAMPTZ NOT NULL DEFAULT now(),
  enabled         BOOLEAN NOT NULL DEFAULT TRUE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (claim_pattern)
);

CREATE INDEX IF NOT EXISTS idx_known_good_claims_project
  ON narasi_known_good_claims (project_id) WHERE scope = 'project';

GRANT SELECT, INSERT, UPDATE, DELETE ON narasi_known_good_claims TO app_user;

-- Seeds: the round-4 verified-good negatives (acceptance §6 negative controls).
INSERT INTO narasi_known_good_claims (claim_pattern, verified_value, epistemic_class, source) VALUES
  ('(?i)\b93\s*days\b',                    '93 days (May 22 - Aug 13, 1521)', 'world',  'siege of Tenochtitlan, verified x4'),
  ('(?i)november\s+8,?\s+1519|8\s+november\s+1519', 'Nov 8, 1519',            'world',  'entry into Tenochtitlan, documented'),
  ('(?i)\bteocuitlatl\b',                  'Nahuatl: "divine excrement"',      'source', 'standard Nahuatl gloss')
ON CONFLICT (claim_pattern) DO NOTHING;

COMMENT ON TABLE narasi_known_good_claims IS
  'R-FG8 verified-claim cache + audit trail: sweep-exempt patterns; future verify pass writes here.';

COMMIT;
