-- 0066_factgate_seed_aceh.sql
-- PIPELINE-SPEC v1 §9 — Aceh review seeds. All facts SEARCH-VERIFIED before landing
-- per §5 seed-verify discipline (the 0063 self-seed hallucination class must not repeat).
-- Sources: Vickers, Rosihan Anwar, van 't Veer (De Atjeh-oorlog), Reid (Asal Mula
-- Konflik Aceh), Lombard (Kerajaan Aceh Iskandar Muda EFEO), Kempees notes (via van 't
-- Veer), Zentgraaff. Every claim is a corrected-verdict on this manuscript's shipped
-- errors OR a reinforced-positive-control that must NOT regress.
BEGIN;

-- ═══ KNOWN_BAD: the round's contradicted claims ═══
INSERT INTO narasi_known_bad_claims (name, bad_pattern, correct_value, action, source, scope) VALUES
  ('teuku-umar-beberapa-bulan-defect',
   '(?i)teuku\s+umar[^.\n]{0,140}?(?P<bad>(?:beberapa\s+bulan|beberapa\s+minggu|dalam\s+hitungan\s+bulan)[^.\n]{0,60}?(?:membelot|berbalik|kembali\s+ke\s+aceh))',
   'Teuku Umar menyerah 30 September 1893, membelot balik 30 Maret 1896 — TIGA TAHUN (bukan beberapa bulan) sambil membawa 800+ senapan & 25.000 peluru. Kesabaran 3-tahun itulah inti Tipu Aceh.', 'flag',
   'aceh review round-1 (van t Veer De Atjeh-oorlog)', 'global'),
  ('deykerhoff-terbalik-posisi',
   '(?i)deij?kerhoff[^.\n]{0,120}?(?P<bad>(?:meragukan|skeptis|sceptis|menentang|menolak)[^.\n]{0,60}?(?:pendekatan\s+lunak|soft\s+approach|kompromi))',
   'Deijkerhoff terbalik: dia gubernur yang MEMPERSENJATAI Teuku Umar, mundur SETELAH terjebak Tipu Aceh — arsitek pendekatan lunak, bukan skeptiknya. Position-inversion (kelas Altman).', 'flag',
   'aceh review, search-verified', 'global'),
  ('kuta-reh-313-as-total',
   '(?i)kuta\s+reh[^.\n]{0,200}?(?P<bad>313[^.\n]{0,60}?(?:penduduk|jiwa|orang|total|korban))',
   '313 itu subtotal PRIA. Catatan Kempees via van t Veer: 561 total (313 laki-laki / 189 perempuan / 59 anak-anak); sumber Aceh 2.922; ekspedisi seluruh ~4.000 (Zentgraaff). Mengutip subtotal sebagai total = auto-undercut poin sendiri.', 'flag',
   'aceh review (Kempees/van t Veer/Zentgraaff)', 'global'),
  ('kuta-reh-gayo-lokasi',
   '(?i)kuta\s+reh[^.\n]{0,120}?(?P<bad>(?:dataran\s+tinggi\s+gayo|pegunungan\s+gayo|tanah\s+gayo))',
   'Kuta Reh di Tanah ALAS (Kec. Bambel, Aceh Tenggara), BUKAN dataran tinggi Gayo. Lembah Alas jauh lebih rendah dari 1000 m.', 'flag',
   'aceh review, search-verified', 'global'),
  ('korban-belanda-75000',
   '(?i)(?:belanda|knil|kolonial)[^.\n]{0,140}?(?P<bad>(?:75[.,]?000|75\s+ribu|tujuh\s+puluh\s+lima\s+ribu|lebih\s+dari\s+seratus\s+ribu)[^.\n]{0,60}?(?:tewas|korban|meninggal))',
   'Korban Belanda ~37.000 total (Vickers: ~2.000 combat + ~35.000 penyakit termasuk buruh; Rosihan Anwar: 35.000 KNIL; range sumber 37-50 ribu). Angka >75.000 dua kali lipat estimasi standar.', 'flag',
   'aceh review (Vickers, Rosihan Anwar)', 'global'),
  ('korban-aceh-100000-angka-resmi',
   '(?i)(?P<bad>(?:angka\s+resmi\s+kolonial|catatan\s+resmi\s+belanda)[^.\n]{0,80}?(?:100[.,]?000|seratus\s+ribu|lebih\s+dari\s+100))',
   'Tidak ada angka resmi kolonial setinggi itu untuk korban Aceh. Estimasi standar 50-70 ribu (Vickers 50-60rb, Rosihan Anwar 70rb ≈ 4% populasi).', 'flag',
   'aceh review, search-verified', 'global'),
  ('concentratie-stelsel-response-umar',
   '(?i)(?:sebagai\s+respons(?:i|)?|jawaban)[^.\n]{0,80}?(?:pengkhianatan|membelot|umar)[^.\n]{0,80}?(?P<bad>(?:concentratie[- ]stelsel|garis\s+konsentrasi|linie))',
   'Concentratie-stelsel/garis konsentrasi dibangun ~1884 (SEBELUM Umar menyerah 1893). Respons AKTUAL atas 1896 = KEBALIKANNYA: ofensif van Heutsz yang MENGAKHIRI kebijakan konsentrasi. Kausal-order terbalik.', 'flag',
   'aceh review, search-verified', 'global'),
  ('masjid-baiturrahman-ekspedisi-kedua',
   '(?i)(?:baiturrahman|masjid\s+raya)[^.\n]{0,200}?(?P<bad>(?:setelah|sesudah|menyusul)[^.\n]{0,60}?(?:ekspedisi\s+kedua|penyerangan\s+kedua))',
   'Masjid Baiturrahman dibakar April 1873 (ekspedisi PERTAMA, sebelum Köhler tewas — kemarahan atas pembakaran itu justru konteks tewasnya Köhler). Sumber tidak seragam soal urutan tapi versi mayoritas = ekspedisi pertama.', 'flag',
   'aceh review, search-verified', 'global'),
  ('senapan-berulang-1873',
   '(?i)(?P<bad>senapan\s+berulang(?:\s+eropa)?[^.\n]{0,60}?(?:1873|187[3-5]|awal\s+perang))',
   'Anakronisme: 1873 = Beaumont single-shot. Repeater militer Eropa baru 1890-an. Sebut "senapan Beaumont" atau "senapan modern Eropa".', 'flag',
   'aceh review, search-verified', 'global')
ON CONFLICT (bad_pattern) DO NOTHING;

-- ═══ KNOWN_GOOD: verified positive controls (do not regress) ═══
INSERT INTO narasi_known_good_claims (claim_pattern, verified_value, epistemic_class, source) VALUES
  ('(?i)teuku\s+umar[^.\n]{0,120}(?:menyerah|beralih)[^.\n]{0,60}(?:30\s+september\s+1893|1893)',
   'Teuku Umar menyerah 30 September 1893, membelot 30 Maret 1896 (~3 tahun), bawa 800+ senapan & 25.000 peluru', 'world',
   'aceh review, van t Veer'),
  ('(?i)k[oö]hler[^.\n]{0,80}(?:14\s+april\s+1873|tewas)',
   'Jenderal Köhler tewas 14 April 1873 di Pante Ceureumen dengan ~3.000 personel', 'world',
   'aceh review, standard historiography'),
  ('(?i)pante\s+ceureumen|pantai\s+cermin',
   'Pantai Ceureumen (Aceh: pante = pantai, ceureumen = cermin); "Pantai Cermin" gloss sah', 'world',
   'aceh review'),
  ('(?i)snouck[^.\n]{0,140}(?:mekah|mecca|abdul\s+ghaffar|1891|1892)',
   'Snouck Hurgronje di Mekah ~5 bulan sebagai Abdul Ghaffar (1884-85), di Aceh 1891-92; kemudian mengutuk aksi van Daalen', 'attribution',
   'aceh review, search-verified'),
  ('(?i)traktat\s+sumatra\s+1871|1871[^.\n]{0,60}sumatra',
   'Traktat SUMATRA 1871 (Belanda-Inggris, membuka jalan invasi Aceh) — BUKAN Traktat London 1824', 'world',
   'aceh review'),
  ('(?i)cut\s+nyak\s+meutia[^.\n]{0,80}(?:1910|wafat|gugur)',
   'Cut Nyak Meutia wafat 1910', 'world', 'aceh review'),
  ('(?i)kerkhof\s+peucut',
   'Kerkhof Peucut Banda Aceh: ~2.200 nisan Belanda + KNIL', 'world', 'aceh review'),
  ('(?i)(?:kuta\s+reh)[^.\n]{0,80}(?:14\s+juni\s+1904|juni\s+1904)',
   'Kuta Reh 14 Juni 1904 (ekspedisi Gayo van Daalen); 561 korban per Kempees (313 L / 189 P / 59 anak); Tanah Alas Aceh Tenggara', 'world',
   'aceh review, van t Veer/Kempees'),
  ('(?i)van\s+heutsz[^.\n]{0,120}(?:gubernur|ofensif|1896|1898|gayo|ekspedisi|gg\s+1904)',
   'Van Heutsz — arsitek ofensif pasca-1896 yang mengakhiri Concentratie; memerintahkan ekspedisi Gayo (dilaksanakan van Daalen); GG Hindia 1904-1909. Pivot antara Snouck dan van Daalen.', 'attribution',
   'aceh review, standard historiography'),
  ('(?i)concentratie[- ]stelsel[^.\n]{0,80}(?:1884|~?1884|van\s+heutsz)',
   'Concentratie-linie/stelsel ~1884; diakhiri ofensif van Heutsz pasca-1896', 'world',
   'aceh review'),
  ('(?i)baiturrahman[^.\n]{0,180}(?:1879|dibangun\s+ulang|belanda\s+membangun)',
   'Masjid Baiturrahman DIBANGUN ULANG oleh Belanda 1879 sebagai gestur konsiliasi — ikon Aceh hari ini dibangun musuhnya (ironi Big History)', 'world',
   'aceh review'),
  ('(?i)snouck[^.\n]{0,180}(?:mengutuk|mengecam|menentang)[^.\n]{0,80}van\s+daalen',
   'Snouck sendiri mengutuk aksi van Daalen yang dinilai melampaui batas — penutup arc "pengetahuan mendahului pedang"', 'attribution',
   'aceh review'),
  ('(?i)(?:400\s+juta|500\s+juta|satu\s+miliar|1\s+miliar)\s+gulden',
   'Biaya Perang Aceh: range estimasi 400 juta - 1 miliar gulden — hedged range PASS', 'scholarly',
   'aceh review, wide historiography'),
  ('(?i)anthony\s+reid|reid[^.\n]{0,80}(?:asal\s+mula|konflik\s+aceh)',
   'Anthony Reid — sejarawan Australia, "Asal Mula Konflik Aceh" (real, on-domain untuk kelas perang Aceh)', 'attribution',
   'aceh review'),
  ('(?i)van\s+.?t\s+veer|paul\s+van\s+.?t\s+veer|de\s+atjeh[- ]oorlog',
   'Paul van t Veer — jurnalis dan sejarawan Belanda, "De Atjeh-oorlog" (1969) = kronik standar Perang Aceh', 'attribution',
   'aceh review'),
  ('(?i)(?:denys\s+)?lombard|lombard[^.\n]{0,100}(?:iskandar\s+muda|efeo|kerajaan\s+aceh)',
   'Denys Lombard — orientalis Prancis EFEO, "Le sultanat d Atjéh au temps d Iskandar Muda" (real, on-domain untuk kelas Aceh era Iskandar Muda)', 'attribution',
   'aceh review')
ON CONFLICT (claim_pattern) DO NOTHING;

COMMIT;
