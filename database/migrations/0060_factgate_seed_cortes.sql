-- 0060_factgate_seed_cortes.sql
-- FG-SEARCH §4: seed the verify cache from the round-2 human-verified Cortés ledger
-- (factgate-ledger-cortes) so the Tenochtitlan corpus is verified-by-cache before the
-- first automated search runs. VERIFIED list → known_good (sweep-exempt, with class +
-- source); CONTRADICTED → known_bad flag-only (siege-93-days replace rule already lives
-- in the 0057 seed). Patterns are PYTHON regex, context-guarded: the subject term and
-- the correct value must co-occur in the sentence, so a WRONG value never gets exempted.
BEGIN;

INSERT INTO narasi_known_good_claims (claim_pattern, verified_value, epistemic_class, source) VALUES
  ('(?i)paris[^.\n]{0,100}\b2[02]5?,?000|\b2[02]5?,?000[^.\n]{0,100}paris',
   'Paris c. 1500 population ~200,000-225,000 (largest city in Europe)', 'world',
   'factgate-ledger-cortes round-2'),
  ('(?i)cocoliztli[^.\n]{0,160}salmonella|salmonella[^.\n]{0,160}cocoliztli',
   '1545-48 cocoliztli epidemic linked to Salmonella enterica Paratyphi C (aDNA)', 'scholarly',
   'Vagene et al. 2018, Nature Ecology & Evolution'),
  ('(?i)earl\s+j?\.?\s*hamilton[^.\n]{0,160}(treasure|gold|silver|bullion|price)',
   'Earl J. Hamilton, American Treasure and the Price Revolution in Spain (1934)', 'attribution',
   'factgate-ledger-cortes round-2'),
  ('(?i)borah[^.\n]{0,80}cook|cook[^.\n]{0,80}borah',
   'Cook & Borah central-Mexico pre-contact population estimates (~25M, contested high bound)', 'attribution',
   'factgate-ledger-cortes round-2'),
  ('(?i)(brigantine|portage)[^.\n]{0,120}\b(50|fifty)\s*(miles|mi)\b',
   'brigantine portage Tlaxcala to Texcoco ~50 miles overland', 'world',
   'factgate-ledger-cortes round-2'),
  ('(?i)cort[eé]s[^.\n]{0,100}\b1547\b|\b1547\b[^.\n]{0,100}cort[eé]s',
   'Cortes died 2 December 1547, Castilleja de la Cuesta, Spain', 'world',
   'factgate-ledger-cortes round-2'),
  ('(?i)hospital\s+de\s+jes[uú]s[^.\n]{0,120}19[43][60]|1946[^.\n]{0,120}hospital\s+de\s+jes[uú]s',
   'Cortes remains located/authenticated 1946-47, Hospital de Jesus, Mexico City', 'world',
   'factgate-ledger-cortes round-2'),
  ('(?i)scuttl\w+[^.\n]{0,80}ships?|ships?[^.\n]{0,80}scuttl\w+',
   'Cortes scuttled (ran aground/sank) his ships at Veracruz 1519 - did NOT burn them', 'world',
   'factgate-ledger-cortes round-2'),
  ('(?i)cuitl[aá]huac[^.\n]{0,120}\b(80|eighty)\s*days?|\b(80|eighty)\s*days?[^.\n]{0,120}cuitl[aá]huac',
   'Cuitlahuac ruled ~80 days, died of smallpox late 1520', 'world',
   'factgate-ledger-cortes round-2'),
  ('(?i)camilla\s+townsend|townsend[^.\n]{0,80}(fifth\s+sun|malintzin|nahua|aztec)',
   'Camilla Townsend - Fifth Sun (2019), Nahua-annals perspective', 'attribution',
   'factgate-ledger-cortes round-2'),
  ('(?i)matthew\s+restall|restall[^.\n]{0,80}(seven\s+myths|montezuma|conquest)',
   'Matthew Restall - Seven Myths of the Spanish Conquest (2003); When Montezuma Met Cortes (2018)', 'attribution',
   'factgate-ledger-cortes round-2'),
  ('(?i)ross\s+hassig|hassig[^.\n]{0,80}(aztec|conquest|warfare|mexico)',
   'Ross Hassig - Aztec Warfare (1988); Mexico and the Spanish Conquest (1994)', 'attribution',
   'factgate-ledger-cortes round-2')
ON CONFLICT (claim_pattern) DO NOTHING;

-- CONTRADICTED → known_bad, flag-only (auto-replace would mangle prose; the ledger's one
-- safe replace — siege 93 days — is already the 0057 seed). Cortés-guarded to avoid
-- flagging other burned ships in history.
INSERT INTO narasi_known_bad_claims (name, bad_pattern, correct_value, action, source, scope) VALUES
  ('ships-burned-myth',
   '(?i)cort[eé]s[^.\n]{0,100}burn\w{0,3}[^.\n]{0,60}ships?|ships?[^.\n]{0,60}burn\w{0,3}[^.\n]{0,100}cort[eé]s',
   'scuttled/ran aground at Veracruz 1519 - the burning is a later myth', 'flag',
   'factgate-ledger-cortes round-2', 'global')
ON CONFLICT (bad_pattern) DO NOTHING;

COMMIT;
