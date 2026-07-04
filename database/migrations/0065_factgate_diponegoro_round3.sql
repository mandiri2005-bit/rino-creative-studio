-- 0065_factgate_diponegoro_round3.sql
-- Round-3 review (8.5/10) meta-finding: a "gap flagged missing" was filled with a
-- HALLUCINATED date because MY OWN 0063 seed carried "Kyai Mojo menyerah/ditangkap
-- Februari 1829" — that was reviewer-memory, not verified fact. Search-verified truth:
-- Kyai Mojo disergap di MLANGI/Sleman 12 November 1828, dibawa ke Salatiga, diasingkan
-- ke Tondano 17 November 1828. This migration CORRECTS THE STORE, not the manuscript.
--
-- LESSON (documented in PIPELINE-SPEC §5): every fact in a seed migration must be
-- search-verified before landing. A wrong known_good is worse than an empty cache: it
-- exempts a real error AND becomes a source of confabulation. Reviewer facts are
-- claims-to-check, not verdicts.
BEGIN;

-- (1) Nuke the wrong 0063 known_good ("Februari 1829"). Plain LIKE on the substring —
-- backslashes inside the stored regex would confuse LIKE ESCAPE, but here we just want
-- ANY known_good whose pattern references Kyai Mojo AND 1829, which is unique to the
-- bad seed (the correct 12-Nov-1828 pattern doesn't mention 1829).
DELETE FROM narasi_known_good_claims
 WHERE claim_pattern LIKE '%kyai%mojo%menyerah%1829%';

-- (2) Add the WRONG date as known_bad so the manuscript never ships it again.
INSERT INTO narasi_known_bad_claims (name, bad_pattern, correct_value, action, source, scope) VALUES
  ('kyai-mojo-menyerah-feb-1829',
   '(?i)kyai\s+mojo[^.\n]{0,140}?(?P<bad>(?:menyerah|menyerahkan\s+diri|ditangkap)[^.\n]{0,60}?(?:februari\s+)?1829)',
   'Kyai Mojo DISERGAP (tidak menyerah) di MLANGI/Sleman pada 12 November 1828; dibawa ke Salatiga; diasingkan ke Tondano 17 November 1828', 'flag',
   'diponegoro-3 review, search-verified (correcting 0063 self-seed)', 'global'),
  ('kyai-mojo-menyerah-1829-spelled',
   '(?i)kyai\s+mojo[^.\n]{0,140}?(?P<bad>(?:menyerah|menyerahkan\s+diri|ditangkap)[^.\n]{0,60}?(?:februari\s+)?seribu\s+delapan\s+ratus\s+dua\s+puluh\s+sembilan)',
   'Kyai Mojo DISERGAP 12 November 1828 di Mlangi (bukan 1829)', 'flag',
   'diponegoro-3 review, search-verified', 'global'),
  -- (3) "Kas Kasasi" - fabricated Dutch-colonial finance term (Rino: kasasi = cassation
  -- legal term, has nothing to do with state coffers). Correct: staatskas / 's Rijks
  -- schatkist. R-FG10 confabulation class.
  ('kas-kasasi-fabricated-term',
   '(?i)(?P<bad>[Kk]as\s+[Kk]asasi)',
   'Istilah karangan: "kasasi" = istilah hukum (cassation), bukan istilah kas negara. Kas negara Belanda = staatskas / s Rijks schatkist', 'flag',
   'diponegoro-3 review, search-verified', 'global'),
  -- (4) Related term-invention: "keuangan negara kolonial" attached to Kasasi.
  ('kasasi-keuangan-negara',
   '(?i)(?P<bad>kasasi[^.\n]{0,60}?keuangan\s+negara)',
   'Konfabulasi istilah: gunakan staatskas / kas Hindia Belanda / gouvernement schatkist', 'flag',
   'diponegoro-3 review, search-verified', 'global')
ON CONFLICT (bad_pattern) DO NOTHING;

-- (5) Correct known_good entries.
INSERT INTO narasi_known_good_claims (claim_pattern, verified_value, epistemic_class, source) VALUES
  ('(?i)kyai\s+mojo[^.\n]{0,140}(?:disergap|ditangkap|dibawa)[^.\n]{0,60}(?:12\s+november\s+1828|mlangi|sleman)',
   'Kyai Mojo disergap di MLANGI/Sleman 12 November 1828; ke Salatiga; asingkan Tondano 17 Nov 1828', 'world',
   'diponegoro-3 review, search-verified'),
  ('(?i)mlangi[^.\n]{0,80}12\s+november\s+1828|12\s+november\s+1828[^.\n]{0,80}(?:mlangi|kyai\s+mojo)',
   '12 November 1828 - penangkapan Kyai Mojo di Mlangi (turning point sebelum Sentot Okt 1829)', 'world',
   'diponegoro-3 review, search-verified'),
  ('(?i)(?:staatskas|s\s+rijks\s+schatkist|gouvernement\s+schatkist)',
   'Kas negara Hindia Belanda: staatskas / s Rijks schatkist / gouvernement schatkist', 'world',
   'diponegoro-3 review, search-verified'),
  ('(?i)sagimun[^.\n]{0,80}(?:mulus\s+dumadi|pahlawan\s+dipanegara)',
   'Sagimun Mulus Dumadi - sejarawan Indonesia, "Pahlawan Dipanegara Berjuang" (1965) - real, on-domain', 'attribution',
   'diponegoro-3 review, search-verified'),
  ('(?i)jan\s+breman[^.\n]{0,100}(?:cultuurstelsel|agraria|amsterdam)|amsterdam[^.\n]{0,80}jan\s+breman',
   'Jan Breman - sosiolog agraria Universitas Amsterdam, argumen Cultuurstelsel - real & on-domain', 'attribution',
   'diponegoro-3 review, search-verified')
ON CONFLICT (claim_pattern) DO NOTHING;

COMMIT;
