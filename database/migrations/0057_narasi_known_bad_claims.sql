-- 0057_narasi_known_bad_claims.sql
-- CC v3 R-FG4 (prior-flag memory): a GLOBAL registry of previously-flagged wrong claims.
-- A human review flag becomes a permanent rule: the narasi gate (python/narasi_gate.py)
-- auto-corrects (action=replace) or flags (action=flag) any regeneration matching
-- bad_pattern, and the corrections are injected into generation prompts as prevention.
-- GLOBAL by design (Rino 2026-07-04): these are universal facts (the siege of
-- Tenochtitlan is 93 days for every tenant) — reference data, not tenant data, so no
-- tenant_id / RLS. The engine also carries an in-process seed fallback, so this table
-- is the durable/extendable registry, not a hard dependency.
-- Additive: nothing breaks if the table is empty or the deploy lands before the code.
BEGIN;

CREATE TABLE IF NOT EXISTS narasi_known_bad_claims (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name          TEXT NOT NULL,
  bad_pattern   TEXT NOT NULL,           -- python regex WITH a named group (?P<bad>...)
  correct_value TEXT NOT NULL DEFAULT '',
  action        TEXT NOT NULL DEFAULT 'replace' CHECK (action IN ('replace','flag')),
  source        TEXT NOT NULL DEFAULT '',
  enabled       BOOLEAN NOT NULL DEFAULT TRUE,
  first_flagged TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (bad_pattern)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON narasi_known_bad_claims TO app_user;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'set_updated_at') THEN
    CREATE TRIGGER trg_narasi_known_bad_claims_updated
      BEFORE UPDATE ON narasi_known_bad_claims
      FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
END $$;

-- Seed (mirrors the in-process fallback in python/narasi_gate.py KNOWN_BAD_SEED):
INSERT INTO narasi_known_bad_claims (name, bad_pattern, correct_value, action, source) VALUES
  ('tenochtitlan-siege-93-days',
   '(?is)(?:siege|demolition|tenochtitlan|blockade)[^.!?\n]{0,200}?(?P<bad>(?:seventy[-\s]?five|seventy[-\s]?nine|75|79|80)[-\s]*(?:days?|hari))\b',
   '93 days', 'replace',
   'human flag x3 (R-FG4 seed); siege = 93 days, May 22 - Aug 13, 1521'),
  ('tenochtitlan-siege-93-days-rev',
   '(?is)\b(?P<bad>(?:seventy[-\s]?five|seventy[-\s]?nine|75|79|80)[-\s]*(?:days?|hari))[^.!?\n]{0,200}?(?:siege|tenochtitlan)',
   '93 days', 'replace',
   'human flag x3 (R-FG4 seed)'),
  ('tlaxcalan-immunity-error',
   '(?is)(?P<bad>\b(?:tlaxcal\w+|totonac\w*|indigenous|native)\b[^.!?\n]{0,120}?(?:immun\w+|survived\s+childhood\s+smallpox|prior\s+exposure|old[-\s]world\s+exposure))',
   'Indigenous allies (Tlaxcalans, Totonacs, etc.) had NO Old World disease exposure or immunity — only Europeans with prior exposure did.',
   'flag',
   'backend-rules fix #2 (R-FG4 seed)')
ON CONFLICT (bad_pattern) DO NOTHING;

COMMENT ON TABLE narasi_known_bad_claims IS
  'R-FG4 prior-flag memory: human-flagged wrong claims become permanent auto-correct/flag rules for the narasi gates (global reference data, no tenant scoping).';

COMMIT;
