-- 0063_factgate_diponegoro_round2.sql
-- Diponegoro-2 review (7.5/10): the seeded facts all landed (Manado, 1973, 380m,
-- primbon-Makassar = 4/4 correct); the remaining errors are facts NOT yet in the cache —
-- exactly the predicted pattern. Seed this round's verified verdicts. Python regex,
-- (?P<bad>) spans, flag-only.
BEGIN;

INSERT INTO narasi_known_bad_claims (name, bad_pattern, correct_value, action, source, scope) VALUES
  ('kyai-mojo-dari-surakarta',
   '(?i)kyai\s+mojo[^.\n]{0,80}(?P<bad>dari\s+(?:wilayah\s+|lingkup\s+)?surakarta)',
   'Kyai Mojo berasal dari desa Mojo, dekat Pajang/Surakarta (bukan "dari Surakarta"); menyerah Februari 1829 - beat besar sebelum Sentot', 'flag',
   'diponegoro-2 review, search-verified', 'global'),
  ('dekock-nama-konfabulasi',
   '(?i)(?P<bad>hendrik\s+merkus\s+de\s+groot\s+van\s+amstel\s+de\s+kock)',
   'Hendrik Merkus (Baron) de Kock - tanpa "de Groot van Amstel" (konfabulasi partikel nama Belanda)', 'flag',
   'diponegoro-2 review, search-verified', 'global'),
  ('buiskool-scholar-fabricated',
   '(?i)(?P<bad>meriel\s+buiskool)',
   'Tidak ada sejarawan Perang Jawa/filolog Kedu bernama Buiskool (Dirk Buiskool = sejarawan Sumatra Utara) - downgrade ke anonim', 'flag',
   'diponegoro-2 review, search-verified', 'global')
ON CONFLICT (bad_pattern) DO NOTHING;

INSERT INTO narasi_known_good_claims (claim_pattern, verified_value, epistemic_class, source) VALUES
  ('(?i)kyai\s+mojo[^.\n]{0,120}menyerah[^.\n]{0,60}(?:februari\s+)?1829|menyerah[^.\n]{0,60}februari\s+1829[^.\n]{0,80}mojo',
   'Kyai Mojo menyerah/ditangkap Februari 1829 (turning point sebelum Sentot Okt 1829)', 'world',
   'diponegoro-2 review'),
  ('(?i)hendrik\s+merkus(?:,?\s+baron)?\s+de\s+kock',
   'Hendrik Merkus (Baron) de Kock - letnan gubernur-jenderal, arsitek Benteng Stelsel', 'world',
   'diponegoro-2 review'),
  ('(?i)(?:±\s*)?100[.,]?000[^.\n]{0,80}(?:pejuang|pengikut)|seratus\s+ribu[^.\n]{0,60}pejuang',
   'Sekitar 100.000 pejuang bergabung dengan Diponegoro (angka Carey)', 'scholarly',
   'diponegoro-2 review'),
  ('(?i)louw[^.\n]{0,60}de\s+klerck|de\s+klerck[^.\n]{0,60}louw',
   'P.J.F. Louw & E.S. de Klerck - dua sejarawan kolonial penyusun kronologi resmi Perang Jawa (De Java-Oorlog)', 'attribution',
   'diponegoro-2 review'),
  ('(?i)(?:perron|michiels)[^.\n]{0,120}(?:tiga\s+hari|disiagakan)|magelang[^.\n]{0,160}(?:perron|michiels)',
   'Perwira Perron & Michiels disiagakan beberapa hari sebelum penangkapan Magelang', 'world',
   'diponegoro-2 review'),
  ('(?i)smissaert',
   'A.H. Smissaert - Residen Yogyakarta saat insiden patok Tegalrejo 1825', 'world',
   'diponegoro-2 review'),
  ('(?i)van\s+den\s+bosch[^.\n]{0,100}cultuurstelsel|cultuurstelsel[^.\n]{0,100}van\s+den\s+bosch',
   'Johannes van den Bosch - penggagas Cultuurstelsel 1830', 'world',
   'diponegoro-2 review'),
  ('(?i)pahlawan\s+nasional[^.\n]{0,60}\b1973\b',
   'Pahlawan nasional 1973 (Keppres 87/TK/1973) - tanggal PASTI, render exact tanpa hedge', 'world',
   'diponegoro-2 review')
ON CONFLICT (claim_pattern) DO NOTHING;

COMMIT;
