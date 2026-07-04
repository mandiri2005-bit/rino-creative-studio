-- 0062_factgate_diponegoro_spelled.sql
-- Post-mortem round 2 (the ACTUAL shipped manuscript): the 0061 patterns assumed digit
-- forms, but ID prose SPELLS its numbers — "enam ratus meter" (Magelang), "sembilan
-- belas empat puluh lima" (pahlawan 1945) — and the Manado/Makassar claims sit ACROSS
-- sentence boundaries so [^.\n] windows never reach them. These rows add spelled +
-- cross-sentence ([\s\S]{0,N}) variants. Python regex, (?P<bad>) spans, flag-only.
BEGIN;

INSERT INTO narasi_known_bad_claims (name, bad_pattern, correct_value, action, source, scope) VALUES
  ('magelang-elevasi-600-spelled',
   '(?i)magelang[\s\S]{0,150}?(?P<bad>enam\s+ratus)\s*(?:m\b|meter|mdpl)',
   'Magelang sekitar 380 mdpl (rentang kota 375-500), bukan enam ratus', 'flag',
   'id-path post-mortem round-2 (shipped ms)', 'global'),
  ('diponegoro-pahlawan-1945-spelled',
   '(?i)diponegoro[\s\S]{0,250}?pahlawan\s+nasional[^.\n]{0,80}(?P<bad>sembilan\s+belas\s+empat\s+puluh\s+lima)',
   'Pahlawan nasional 1973 (Keppres 87/TK/1973, 6 Nov), bukan 1945', 'flag',
   'id-path post-mortem round-2 (shipped ms)', 'global'),
  ('diponegoro-manado-enam-tahun-xsent',
   '(?i)manado[\s\S]{0,350}?(?P<bad>(?:sekitar\s+|selama\s+)?enam\s+tahun)',
   'Pengasingan Manado sekitar 3 tahun (1830-1833), bukan enam', 'flag',
   'id-path post-mortem round-2 (shipped ms)', 'global'),
  ('babad-makassar-composition-xsent',
   '(?i)(?:fort\s+rotterdam|makassar)[\s\S]{0,350}?(?P<bad>(?:mulai\s+)?menulis[\s\S]{0,100}?babad\s+diponegoro)',
   'Babad Diponegoro ditulis/didiktekan di MANADO (Mei 1831-Feb 1832); output Makassar = 2 notebook primbon tasawuf', 'flag',
   'id-path post-mortem round-2 (shipped ms)', 'global'),
  ('diponegoro-makassar-arrival-1831',
   '(?i)makassar[\s\S]{0,300}?(?P<bad>delapan\s+belas\s+tiga\s+puluh\s+satu|\b1831\b)',
   'Pindah ke Makassar/Fort Rotterdam 1833 (Manado dulu, 1830-1833)', 'flag',
   'id-path post-mortem round-2 (shipped ms)', 'global')
ON CONFLICT (bad_pattern) DO NOTHING;

COMMIT;
