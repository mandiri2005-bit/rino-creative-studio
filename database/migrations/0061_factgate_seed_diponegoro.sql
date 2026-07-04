-- 0061_factgate_seed_diponegoro.sql
-- ID-path fixes §8: seed the fact-gate cache from the Diponegoro review (all values
-- search-verified during the post-mortem). Same contract as 0060 (Cortés): patterns are
-- PYTHON regex, context-guarded so a WRONG value never gets exempted; known_bad rows
-- carry (?P<bad>) spans (the gate matches m.group("bad") — rows without it are dropped
-- at load). Flag-only actions: prose replacement is the editor pass's job.
BEGIN;

INSERT INTO narasi_known_bad_claims (name, bad_pattern, correct_value, action, source, scope) VALUES
  ('babad-diponegoro-makassar-composition',
   '(?i)babad\s+diponegoro[^.\n]{0,160}(?:ditulis|didiktekan|disusun|digubah)[^.\n]{0,80}(?P<bad>fort\s+rotterdam|makassar|ujung\s*pandang)',
   'Babad Diponegoro ditulis/didiktekan di MANADO, Mei 1831-Feb 1832. Output Makassar = 2 notebook primbon tentang tasawuf (fakta yang benar dan lebih menarik)', 'flag',
   'id-path post-mortem, search-verified', 'global'),
  ('babad-diponegoro-jilid',
   '(?i)babad[^.\n]{0,100}(?P<bad>empat\s*belas\s+jilid|14\s+jilid)',
   '1.151 halaman folio (beberapa sumber 1.170), bukan "empat belas jilid"', 'flag',
   'id-path post-mortem, search-verified', 'global'),
  ('diponegoro-pahlawan-1945',
   '(?i)diponegoro[^.\n]{0,120}pahlawan\s+nasional[^.\n]{0,60}(?P<bad>19[456][0-9])',
   'Pahlawan nasional 1973 (Keppres 87/TK/1973, 6 Nov). Haul Nasional 1955 itu nyata - kemungkinan bibit konfabulasinya', 'flag',
   'id-path post-mortem, search-verified', 'global'),
  ('diponegoro-pengasingan-manado-enam-tahun',
   '(?i)(?:pengasingan|diasingkan)[^.\n]{0,80}manado[^.\n]{0,80}(?P<bad>enam\s+tahun|6\s+tahun)',
   'Pengasingan Manado sekitar 3 tahun (1830-1833), bukan enam', 'flag',
   'id-path post-mortem, search-verified', 'global'),
  ('magelang-elevasi-600',
   '(?i)magelang[^.\n]{0,100}(?P<bad>600)\s*(?:m\b|meter|mdpl)',
   'Magelang sekitar 380 mdpl (rentang kota 375-500), bukan 600', 'flag',
   'id-path post-mortem, search-verified', 'global')
ON CONFLICT (bad_pattern) DO NOTHING;

INSERT INTO narasi_known_good_claims (claim_pattern, verified_value, epistemic_class, source) VALUES
  ('(?i)patok[^.\n]{0,80}tegalrejo[^.\n]{0,60}(?:juli\s+)?1825|tegalrejo[^.\n]{0,80}juli\s+1825',
   'Insiden patok jalan Tegalrejo, Juli 1825 (pemicu perang)', 'world', 'id-path post-mortem'),
  ('(?i)selarong[^.\n]{0,80}(?:markas|basis|gua)|(?:markas|gua)[^.\n]{0,60}selarong',
   'Gua Selarong = markas awal Diponegoro', 'world', 'id-path post-mortem'),
  ('(?i)ratu\s+adil[^.\n]{0,120}(?:jayabaya|ramalan)|jayabaya[^.\n]{0,100}(?:abad\s+ke-?12|ramalan)',
   'Ratu Adil + ramalan Jayabaya (abad-12); legitimasi ganda Ratu Adil + jihad = tesis Carey', 'scholarly', 'id-path post-mortem'),
  ('(?i)kyai\s+mojo[^.\n]{0,80}surakarta|surakarta[^.\n]{0,60}kyai\s+mojo',
   'Kyai Mojo berasal dari lingkup Surakarta', 'world', 'id-path post-mortem'),
  ('(?i)sentot[^.\n]{0,120}(?:dua\s*puluh|20|belum\s+genap)[^.\n]{0,60}tahun|sentot[^.\n]{0,100}menyerah[^.\n]{0,60}(?:oktober\s+)?1829',
   'Sentot Alibasyah <20 tahun saat memimpin; menyerah Oktober 1829', 'world', 'id-path post-mortem'),
  ('(?i)de\s+kock[^.\n]{0,120}(?:benteng\s*stelsel|1827)|benteng\s*stelsel[^.\n]{0,160}(?:1827|de\s+kock|~?200|dua\s+ratus)',
   'De Kock 1827: Benteng Stelsel + kolom gerak cepat, sekitar 200 pos', 'world', 'id-path post-mortem'),
  ('(?i)malaria[^.\n]{0,120}(?:lebih|melebihi|daripada)[^.\n]{0,60}(?:pertempuran|tempur|combat)|malaria[^.\n]{0,80}knil',
   'Kematian KNIL karena malaria/penyakit > tewas tempur', 'scholarly', 'id-path post-mortem'),
  ('(?i)28\s+maret\s+1830[^.\n]{0,80}magelang|magelang[^.\n]{0,80}28\s+maret\s+1830',
   'Penangkapan di Magelang 28 Maret 1830; dua syarat Diponegoro (gelar Sultan + otoritas keagamaan); kontroversi internal Belanda', 'world', 'id-path post-mortem'),
  ('(?i)(?:20|dua\s*puluh)\s+juta\s+gulden',
   'Biaya perang bagi Belanda sekitar 20 juta gulden', 'world', 'id-path post-mortem'),
  ('(?i)cultuurstelsel[^.\n]{0,160}(?:perang\s+jawa|diponegoro|1830)|(?:perang\s+jawa|diponegoro)[^.\n]{0,120}cultuurstelsel',
   'Perang Jawa mendorong Cultuurstelsel (dengan counter-argumen van Niel/Du Bus = historiografi riil)', 'scholarly', 'id-path post-mortem'),
  ('(?i)(?:wafat|meninggal)[^.\n]{0,60}8\s+januari\s+1855|8\s+januari\s+1855[^.\n]{0,80}(?:fort\s+rotterdam|diponegoro)',
   'Diponegoro wafat 8 Januari 1855 di Fort Rotterdam, usia 69', 'world', 'id-path post-mortem'),
  ('(?i)(?:pegon)[^.\n]{0,80}(?:macapat|tembang)|(?:macapat|tembang)[^.\n]{0,80}pegon',
   'Babad Diponegoro: aksara pegon + bentuk macapat/tembang', 'world', 'id-path post-mortem'),
  ('(?i)(?:juru\s+tulis|dipowiyono)[^.\n]{0,120}(?:dipowiyono|babad|diponegoro)',
   'Juru tulis Tumenggung Dipowiyono; keraguan Carey soal kepengarangan fisik', 'scholarly', 'id-path post-mortem'),
  ('(?i)(?:±\s*)?200[.,]?000[^.\n]{0,80}(?:jawa|tewas)|dua\s+ratus\s+ribu[^.\n]{0,60}(?:jawa|tewas)',
   'ANGKA TESIS: ±200.000 orang Jawa tewas', 'world', 'id-path post-mortem'),
  ('(?i)15[.,]?000[^.\n]{0,100}(?:tentara|serdadu|pemerintah)|lima\s+belas\s+ribu[^.\n]{0,80}tentara',
   'ANGKA TESIS: ±15.000 tentara pemerintah tewas (8.000 Eropa + 7.000 pribumi)', 'world', 'id-path post-mortem'),
  ('(?i)(?:2|dua)\s+juta[^.\n]{0,80}(?:terdampak|penduduk|jiwa)|sepertiga[^.\n]{0,60}populasi\s+jawa',
   'ANGKA TESIS: ±2 juta terdampak (sepertiga populasi Jawa)', 'world', 'id-path post-mortem'),
  ('(?i)peter\s+carey|carey[^.\n]{0,100}(?:arsip\s+yogyakarta|empat\s+dekade|perang\s+jawa)',
   'Peter Carey - sejarawan Inggris (Oxford), arsip Yogyakarta, otoritas Perang Jawa', 'attribution', 'id-path post-mortem'),
  ('(?i)m\.?\s*c\.?\s*ricklefs|ricklefs[^.\n]{0,100}(?:islamisasi|australia|jawa)',
   'M.C. (Merle Calvin) Ricklefs - sejarawan Australia, islamisasi Jawa', 'attribution', 'id-path post-mortem')
ON CONFLICT (claim_pattern) DO NOTHING;

COMMIT;
