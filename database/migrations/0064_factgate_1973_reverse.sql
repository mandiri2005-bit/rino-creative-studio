-- 0064_factgate_1973_reverse.sql
-- 0063's pahlawan-1973 known_good only matched "pahlawan nasional … 1973"; the real
-- manuscript says "Pada tahun 1973, pemerintah … menetapkan … pahlawan nasional"
-- (reverse order) so the exact-render dehedge never fired. Reverse-order variant.
BEGIN;
INSERT INTO narasi_known_good_claims (claim_pattern, verified_value, epistemic_class, source) VALUES
  ('(?i)\b1973\b[^.\n]{0,100}pahlawan\s+nasional',
   'Pahlawan nasional 1973 (Keppres 87/TK/1973) - tanggal PASTI, render exact', 'world',
   'diponegoro-2 review (reverse order)')
ON CONFLICT (claim_pattern) DO NOTHING;
COMMIT;
