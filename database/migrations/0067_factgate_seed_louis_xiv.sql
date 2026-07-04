-- 0067_factgate_seed_louis_xiv.sql
-- PIPELINE-SPEC v1 §12.3 — Louis XIV review seeds. All facts VERIFIED via adversarial
-- multi-agent audit (22 REFUTED, 2 CONFIRMED_WRONG, 0 AMBIGUOUS) plus spec §12.3 own
-- reviewer additions. Sources: Saint-Simon Mémoires (eyewitness); François Bluche,
-- Louis XIV (Fayard 1986); Jean-Christian Petitfils, Louis XIV (Perrin 1995); Château
-- de Versailles official documentation. Every claim is a corrected verdict on this
-- manuscript's shipped errors OR a reinforced positive control that must NOT regress.
BEGIN;

-- ═══ KNOWN_BAD: 4 verified contradictions from the manuscript ═══
INSERT INTO narasi_known_bad_claims (name, bad_pattern, correct_value, action, source, scope) VALUES
  ('louis-xv-cinq-ans-1712',
   '(?is)(?:1712|f[ée]vrier\s+1712|onze\s+mois\s+plus\s+tard)[\s\S]{0,600}?(?P<bad>arri[eè]re-?petit-?fils\s+de\s+cinq\s+ans)',
   'Le futur Louis XV (né 15 février 1710) avait DEUX ANS en février 1712, non cinq. La confusion vient du chapitre 7 (succession 1715 → âge 5 = correct); cross-scene arithmetic drift class (§3.3). Utiliser: "arrière-petit-fils de deux ans" en février 1712.', 'flag',
   'louis xiv review, saint-simon + bluche + petitfils', 'global'),
  ('louis-xiv-cortege-quatre-jours',
   '(?is)(?:cortege|convoi|cercueil|d[ée]part)[^.\n]{0,300}?(?:9\s+septembre\s+1715|9\s+sept\.\s*1715)[^.\n]{0,80}?(?P<bad>quatre\s+jours\s+apr[eè]s\s+(?:le\s+)?(?:dernier\s+souffle|d[ée]c[eè]s|mort))',
   'Louis XIV est mort le 1er septembre 1715 (consensus historiographique); le cortège a quitté Versailles la nuit du 8-9 septembre 1715. Intervalle = HUIT JOURS, pas quatre. Erreur arithmétique interne (§3.3 interval-vs-dates).', 'flag',
   'louis xiv review (saint-simon, bluche 1986)', 'global'),
  ('louis-xiii-vers-14-mai-1643',
   '(?is)Louis\s+XIII\s+(?:meurt|est\s+mort|d[ée]c[ée]d[ée]e?)\s+(?P<bad>vers\s+le\s+14\s+mai\s+1643)',
   'Louis XIII est mort le 14 mai 1643 (date EXACTE, universellement documentée à Saint-Germain-en-Laye). "Vers" est un hedge interdit sur une date world-documented (§3.4.2). Utiliser: "le 14 mai 1643", sans hedge.', 'flag',
   'louis xiv review, standard historiography', 'global'),
  ('bourgogne-1712-inversion-vingt-jours',
   '(?is)f[ée]vrier\s+1712[^.\n]{0,300}?(?P<bad>(?:la\s+rougeole\s+emporte\s+)?le\s+duc\s+de\s+Bourgogne[^.\n]{0,80}?(?:puis\s+la\s+duchesse|puis\s+sa\s+duchesse)[^.\n]{0,80}?(?:puis\s+leur\s+fils\s+a[iî]n[ée]|puis\s+leur\s+fils))',
   'Ordre INVERSÉ. Séquence documentée: (1) Marie-Adélaïde de Savoie, DUCHESSE de Bourgogne †12 février 1712; (2) Louis, DUC de Bourgogne †18 février 1712; (3) Louis, duc de BRETAGNE (leur fils aîné) †8 mars 1712. Rentang = VINGT-CINQ jours, pas vingt. Reformuler: "la rougeole emporte la duchesse de Bourgogne (12 février), puis le duc (18 février), puis leur fils aîné le duc de Bretagne (8 mars) — trois cercueils en vingt-cinq jours."', 'flag',
   'louis xiv review, saint-simon + bluche + petitfils', 'global'),
  ('cour-avril-1682-versailles',
   '(?is)(?:cour|court|d[ée]placement)[^.\n]{0,120}?(?P<bad>(?:en|d[eè]s|le)\s+avril\s+1682[^.\n]{0,80}?(?:Versailles|palais|installation))',
   'Le transfert officiel de la cour à Versailles est le 6 MAI 1682, pas avril (Bluche 1986; Petitfils 1995). Utiliser: "le 6 mai 1682" ou "au printemps 1682". Détail secondaire mais vérifiable.', 'flag',
   'louis xiv review, bluche 1986', 'global')
ON CONFLICT (bad_pattern) DO NOTHING;

-- ═══ KNOWN_GOOD: verified positive controls (do not regress) ═══
INSERT INTO narasi_known_good_claims (claim_pattern, verified_value, epistemic_class, source) VALUES
  ('(?i)louis\s+xiv[^.\n]{0,80}(?:1er?\s+septembre\s+1715|1\s+septembre\s+1715|d[ée]c[eè]s\s+1715)',
   'Louis XIV †1er septembre 1715 à Versailles (consensus); cortège 9 sept vers Saint-Denis', 'world',
   'louis xiv review, saint-simon + bluche'),
  ('(?i)louis\s+xiii[^.\n]{0,80}14\s+mai\s+1643',
   'Louis XIII †14 mai 1643 à Saint-Germain-en-Laye (date exacte, JAMAIS hedged §3.4.2)', 'world',
   'louis xiv review, standard historiography'),
  ('(?i)mazarin[^.\n]{0,120}(?:9\s+mars\s+1661|mars\s+1661|1661)',
   'Cardinal Mazarin †9 mars 1661 à Vincennes; ~18 ans de gouvernement effectif (1643-1661)', 'world',
   'louis xiv review'),
  ('(?i)galerie\s+des\s+glaces[^.\n]{0,120}(?:1684|357\s+miroirs|17\s+arcades)',
   'Galerie des Glaces achevée 1684, 357 miroirs face à 17 arcades (Château officialdocs)', 'world',
   'louis xiv review, château versailles'),
  ('(?i)louis\s+xiv[^.\n]{0,80}(?:72\s+(?:ans|anos)|soixante-douze\s+ans)',
   'Règne 72 ans 110 jours (14 mai 1643 → 1er sept 1715); calcul confirmé', 'world',
   'louis xiv review'),
  ('(?i)fouquet[^.\n]{0,120}(?:nantes|d.?artagnan|arr[eê]t[ée])',
   'Nicolas Fouquet arrêté à Nantes par d''Artagnan (capitaine-lieutenant des mousquetaires), septembre 1661', 'world',
   'louis xiv review'),
  ('(?i)vauban[^.\n]{0,120}(?:100\s+places?\s+fortes?|40\s+si[eè]ges?|tranch[ée]es\s+parall[eè]les)',
   'Vauban: ~100+ places fortes construites/remaniées; ~40 sièges dans sa carrière; méthode tranchées parallèles', 'attribution',
   'louis xiv review'),
  ('(?i)louvois[^.\n]{0,180}(?:300[.\s]000|400[.\s]000|arm[ée]e\s+permanente)',
   'Louvois: armée française atteint 300 000-400 000 hommes dans les années 1690 (Lynn, Giant of the Grand Siècle)', 'attribution',
   'louis xiv review, lynn'),
  ('(?i)louis\s+xv[^.\n]{0,80}(?:cinq\s+ans|5\s+ans)[^.\n]{0,80}(?:1715|succession|h[ée]rite)',
   'Le futur Louis XV (né 15 fév 1710) avait 5 ans à la succession en septembre 1715 — usage CORRECT', 'world',
   'louis xiv review'),
  ('(?i)marie[- ]ad[ée]la[iï]de[^.\n]{0,120}(?:12\s+f[ée]vrier\s+1712|savoie|duchesse\s+de\s+bourgogne)',
   'Marie-Adélaïde de Savoie, duchesse de Bourgogne, †12 février 1712 (rougeole) — première morte de la série', 'world',
   'louis xiv review, saint-simon'),
  ('(?i)louis[^.\n]{0,80}duc\s+de\s+bourgogne[^.\n]{0,120}(?:18\s+f[ée]vrier\s+1712|bourgogne\s+†)',
   'Louis, duc de Bourgogne, †18 février 1712 — six jours après son épouse', 'world',
   'louis xiv review'),
  ('(?i)(?:louis[^.\n]{0,40})?duc\s+de\s+bretagne[^.\n]{0,120}(?:8\s+mars\s+1712|bretagne\s+†)',
   'Louis, duc de Bretagne (fils aîné), †8 mars 1712 — dernier de la série', 'world',
   'louis xiv review'),
  ('(?i)maintenon[^.\n]{0,120}(?:mariage\s+secret|vraisemblablement\s+1683|mariage[^.\n]{0,20}1683)',
   'Mariage secret Louis XIV × Mme de Maintenon: date incertaine (~1683-1684, non documentée officiellement); "vraisemblablement 1683" est un hedge LÉGITIME sur date genuinely-uncertain (§3.4.2 exemption)', 'scholarly',
   'louis xiv review'),
  ('(?i)6\s+mai\s+1682[^.\n]{0,80}versailles|versailles[^.\n]{0,80}6\s+mai\s+1682',
   'Cour transférée officiellement à Versailles le 6 mai 1682', 'world',
   'louis xiv review, bluche')
ON CONFLICT (claim_pattern) DO NOTHING;

COMMIT;
