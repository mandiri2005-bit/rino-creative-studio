-- 0068_factgate_seed_bonjol.sql
-- PIPELINE-SPEC v1 §12.1 — Bonjol review seeds. Cache-empty run on Perang Padri.
-- All facts search-verified per §5 seed-verify discipline. Sources: Jeffrey Hadler,
-- Muslims and Matriarchs: Cultural Resilience in Minangkabau through Jihad and
-- Colonialism (Cornell 2008; author affiliation UC Berkeley History); Christine Dobbin,
-- Islamic Revivalism in a Changing Peasant Economy (Curzon 1983); Taufik Abdullah,
-- Schools and Politics: The Kaum Muda Movement in West Sumatra. Every claim is a
-- corrected verdict OR a reinforced positive control that must NOT regress.
BEGIN;

-- ═══ KNOWN_BAD: 5 verified errors + 1 fabricated-scholar entry ═══
INSERT INTO narasi_known_bad_claims (name, bad_pattern, correct_value, action, source, scope) VALUES
  ('hadler-universitas-virginia',
   '(?is)(?:jeffrey\s+)?hadler[^.\n]{0,180}?(?P<bad>universitas\s+virginia|university\s+of\s+virginia|UVA)',
   'Jeffrey Hadler adalah sejarawan UC BERKELEY (Department of South & Southeast Asian Studies), BUKAN Universitas Virginia. Author of "Muslims and Matriarchs: Cultural Resilience in Minangkabau through Jihad and Colonialism" (Cornell UP 2008). Affiliation field load-bearing (§3.2) — real scholar wrong institution = ships silently otherwise.', 'flag',
   'bonjol review, hadler CV verified', 'global'),
  ('murkalala-firman-phantom',
   '(?is)(?P<bad>(?:sejarawan\s+|peneliti\s+)?[Mm]urkalala\s+[Ff]irman[^.\n]{0,120}?(?:minangkabau|padri|bonjol|sumatra))',
   'phantom scholar — nama "Murkalala Firman" tidak muncul di historiografi Minangkabau (dua pencarian independen mengembalikan nol). Kelas Budiardjo-fiktif / Buiskool-fiktif. Downgrade ke "sejumlah peneliti" atau hilangkan atribusi. Fabricated-authority = model failure mode berulang (§3.7 attribution-verify).', 'flag',
   'bonjol review, search-verified null', 'global'),
  ('dobbin-hadler-position-swap',
   '(?is)(?:sejarawan\s+)?(?:christine\s+)?dobbin[^.\n]{0,240}?(?P<bad>(?:kopi(?:\s+dan\s+kassia)?|kassia|garis\s+patahan\s+ekonomi|economic\s+fault[- ]line|ekonomi\s+kolonial))',
   'Position-swap: tesis garis-patahan-ekonomi kopi/kassia adalah tanda tangan HADLER (Muslims and Matriarchs), bukan Dobbin. Dobbin fokus ekonomi pra-kolonial + revivalisme Islam. Menghubungkan ke Dobbin = inversi posisi (kelas Altman/Cortés dan Deykerhoff/Aceh — instance ke-3 lintas 3 topik/2 bahasa). Perbaiki: atribusikan kopi/kassia ke Hadler.', 'flag',
   'bonjol review, hadler + dobbin corpus', 'global'),
  ('puncak-pato-sequence-inversion',
   '(?is)(?:sumpah\s+satie\s+bukik\s+marapalam|plakat\s+puncak\s+pato|puncak\s+pato)[^.\n]{0,300}?(?P<bad>(?:sebelum|mendahului)[^.\n]{0,80}?(?:adat\s+meng?undang\s+belanda|adat\s+menerima\s+belanda|kaum\s+adat\s+meminta|1821))',
   'Urutan kausal TERBALIK. Urutan standar: (1) adat mengundang Belanda 1821 → (2) Belanda memperluas kekuasaan → (3) rekonsiliasi adat-Padri di Puncak Pato ~1833 → (4) perlawanan bersatu → (5) Bonjol jatuh 16 Agu 1837. Kelas sequence-inversion Concentratie/Aceh (§3.1 sequence modifier). Perbaiki urutan atau tanda ambang penanggalan Puncak Pato memang diperdebatkan.', 'flag',
   'bonjol review, taufik abdullah + hadler', 'global'),
  ('bonjol-arc-endpoint-missing',
   '(?is)(?:tuanku\s+imam\s+bonjol|bonjol)[^.\n]{0,600}?(?P<bad>(?:pahlawan\s+nasional|1973)[^.\n]{0,120}?(?:\.|\n|$))',
   'R-H8 arc-endpoint (§3.9) tidak lengkap. Manuskrip person-titled ("pahlawan atau musuh") HARUS memuat penangkapan lewat jebakan meja-perundingan (Oct 1837, Palupuh), pembuangan (Cianjur → Ambon → Lotta, Minahasa), wafat 6 Nov 1864 (Lotta). Ending pengkhianatan-perundingan berima struktural dengan Diponegoro (Magelang). Wajib tulis endpoint sebelum melompat ke "pahlawan nasional 1973".', 'flag',
   'bonjol review, standard biography', 'global'),
  ('bonjol-over-attribution-founder',
   '(?is)(?P<bad>(?:pemimpin\s+yang\s+memulainya|pendiri\s+gerakan\s+padri|penggagas\s+padri|memulai\s+perang\s+padri))',
   'Over-attribution. Tiga haji (Miskin, Sumanik, Piobang, ~1803-04) + Tuanku Nan Renceh mendahului Bonjol; Nan Renceh yang MENAMAI Bonjol sebagai Imam. Bonjol bukan pemulai, dia adalah tokoh sentral fase MATANG. Perbaiki: sebutkan tiga haji + Nan Renceh terlebih dahulu.', 'flag',
   'bonjol review, hadler + dobbin', 'global')
ON CONFLICT (bad_pattern) DO NOTHING;

-- ═══ KNOWN_GOOD: verified positive controls (do not regress) ═══
INSERT INTO narasi_known_good_claims (claim_pattern, verified_value, epistemic_class, source) VALUES
  ('(?i)(?:jeffrey\s+)?hadler[^.\n]{0,200}(?:uc\s+berkeley|berkeley|university\s+of\s+california)',
   'Jeffrey Hadler — sejarawan UC Berkeley (Dept of South & Southeast Asian Studies); author "Muslims and Matriarchs" (Cornell 2008)', 'attribution',
   'bonjol review, hadler CV'),
  ('(?i)(?:christine\s+)?dobbin[^.\n]{0,240}(?:ekonomi\s+pra-?kolonial|revivalisme\s+islam|islamic\s+revivalism|peasant\s+economy)',
   'Christine Dobbin — "Islamic Revivalism in a Changing Peasant Economy" (Curzon 1983); domain: ekonomi pra-kolonial Minangkabau + revivalisme Islam (bukan kopi/kassia)', 'attribution',
   'bonjol review'),
  ('(?i)tiga\s+haji|haji\s+miskin|haji\s+sumanik|haji\s+piobang',
   'Tiga haji (Miskin, Sumanik, Piobang) kembali dari Mekah ~1803-1804 membawa reformisme Wahabi — pemula gerakan Padri, MENDAHULUI Bonjol', 'world',
   'bonjol review, standard historiography'),
  ('(?i)tuanku\s+nan\s+renceh',
   'Tuanku Nan Renceh — tokoh Padri Kamang, MENAMAI Bonjol sebagai Imam; mendahului Bonjol sebagai pemimpin militan', 'world',
   'bonjol review'),
  ('(?i)voc[^.\n]{0,80}(?:1799|dibubarkan)',
   'VOC dibubarkan 1799 (manuskrip "1799-1800" acceptable karena likuidasi berlanjut ke 1800)', 'world',
   'bonjol review'),
  ('(?i)(?:masuk|kembali|kembalinya|kedatangan)[^.\n]{0,80}belanda[^.\n]{0,80}(?:1821|1822)',
   'Kekuasaan Belanda kembali ke Sumatra Barat 1821-22 pasca-Napoleon', 'world',
   'bonjol review'),
  ('(?i)bonjol[^.\n]{0,80}(?:16\s+agustus\s+1837|jatuh\s+1837|16\s+ags\s+1837)',
   'Benteng Bonjol jatuh 16 Agustus 1837', 'world',
   'bonjol review'),
  ('(?i)(?:tuanku\s+imam\s+)?bonjol[^.\n]{0,180}(?:palupuh|jebakan\s+perundingan|oktober\s+1837)',
   'Bonjol ditangkap Oktober 1837 di Palupuh melalui jebakan meja-perundingan — struktural rhyme dengan Magelang/Diponegoro', 'world',
   'bonjol review'),
  ('(?i)(?:tuanku\s+imam\s+)?bonjol[^.\n]{0,180}(?:cianjur|ambon|lotta|minahasa|1864|6\s+nov)',
   'Bonjol dibuang Cianjur → Ambon → Lotta (Minahasa); wafat 6 November 1864 di Lotta', 'world',
   'bonjol review'),
  ('(?i)plakat\s+puncak\s+pato|sumpah\s+satie\s+bukik\s+marapalam',
   'Plakat Puncak Pato / Sumpah Satie Bukik Marapalam: "adat basandi syarak, syarak basandi kitabullah"; penanggalan diperdebatkan (~1833 mayoritas; tradisi klaim lebih tua) — render dengan kontestasi, bukan tanggal pasti', 'attribution',
   'bonjol review'),
  ('(?i)(?:sampai|hingga|up\s+to|around)\s+20[.,]?000[^.\n]{0,80}(?:sipil|korban|jiwa|tewas)',
   'Korban sipil Perang Padri diperkirakan hingga 20.000 (angka standar buku teks, hedged range PASS)', 'scholarly',
   'bonjol review'),
  ('(?i)(?:pahlawan\s+nasional|1973)[^.\n]{0,80}(?:bonjol|imam\s+bonjol)',
   'Bonjol ditetapkan Pahlawan Nasional 1973 (SK Presiden — real, tapi hanya BOLEH ditulis SETELAH arc-endpoint lengkap §3.9 R-H8)', 'world',
   'bonjol review'),
  ('(?i)naali\s+sutan\s+chaniago|(?:disusun|dikompilasi)[^.\n]{0,80}anaknya',
   'Otobiografi Jawi Bonjol dikompilasi anaknya Naali Sutan Chaniago (nuansa authorship — hedge acceptable)', 'attribution',
   'bonjol review')
ON CONFLICT (claim_pattern) DO NOTHING;

COMMIT;
