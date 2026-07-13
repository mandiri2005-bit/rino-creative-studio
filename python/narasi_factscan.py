# ── narasi_factscan — R-FG9 coverage sweep + R-FG10 non-quantitative claim classes.
# (specs: cc-instruksi-fact-gate-typed-verification.md + -nonquant-claims.md)
#
# HONEST SCOPE (v1): the engine has NO web-search infrastructure and NO claim ledger,
# so the R-FG8 *verification* protocols cannot run yet. Per the R-FG10 spec's own ship
# order ("scan-and-report first, block second"), this module is a DETERMINISTIC
# detector suite: it sweeps the final manuscript, classifies claims by epistemic
# class, applies the hard exemptions, consults known_good_claims (noise damping +
# future cache), and emits a `fact_report` — counts + samples per class, never a
# block, zero LLM calls. When a search pass lands, its verdicts consume this same
# report and write known_good entries.
#
#   R-FG9  numeric coverage sweep — digits, spelled numbers, quantity phrases; every
#          hit is an unverified-quantitative claim unless whitelisted (headings,
#          header word-count, idioms) or matched by a known_good pattern.
#   R-FG10 wave 1: negative_existence (unscoped absolutes) + causal_mechanism
#          (unhedged, unattributed causal verbs)
#          wave 2: attributed_quote (historical figure + speech verb) + proper_relation
#          wave 3: etymology_gloss + superlative_qualitative
#          (anachronism_term needs model judgment → deferred, stated in the report)
#   Hard exemptions (§2): conditional-mood atmospherics, hedged interiority /
#          marked unknowables, idiom whitelist.
from __future__ import annotations

import contextvars
import re
from typing import Any, Optional

__all__ = ["fact_scan", "set_known_good"]

_SENT = re.compile(r"(?<=[.!?])\s+")

# ── R-FG9: numeric tokens ──
_DIGITS = re.compile(r"\b\d[\d,.]*\s*(?:%|percent|km|kilometers?|kilometres?|m\b|metres?|meters?|"
                     r"miles?|feet|ft|hectares?|acres?|tons?|pesos?|ducats?|pounds?|lbs?|kg|"
                     r"people|men|soldiers|warriors|inhabitants|traders|days?|years?|hari|kata|orang)?")
_SPELLED = re.compile(r"(?i)\b(?:(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
                      r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million)"
                      r"(?:[-\s](?:one|two|three|four|five|six|seven|eight|nine|hundred|thousand|million))*)"
                      r"\s+(?:thousand|million|hundred|men|soldiers|warriors|people|inhabitants|traders|"
                      r"days?|years?|miles?|paces?|leagues?|ships?|horses?|kilometres?|kilometers?|metres?|meters?)\b")
_YEAR = re.compile(r"\b1[0-9]{3}\b|\b20[0-2][0-9]\b")

# idiom whitelist (grows like the aporia list — spec §7: noisy first run, quiet third)
_IDIOMS = re.compile(r"(?i)\b(?:one\s+thing\s+(?:was|is)\s+clear|first\s+light|one\s+by\s+one|"
                     r"at\s+one\s+point|no\s+one|one\s+of\s+the|never\s+mind|one\s+more)\b")

# ── hard exemptions (§2) ──
_CONDITIONAL = re.compile(r"(?i)\b(?:would\s+have|might\s+have|may\s+have|perhaps|likely|probably|"
                          r"could\s+have|seems?\s+to\s+have)\b")
_UNKNOWABLE = re.compile(r"(?i)\b(?:no\s+one\s+recorded|no\s+source\s+records|history\s+does\s+not|"
                         r"the\s+sources?\s+do(?:es)?\s+not)\b")

# ── R-FG10 detectors ──
_ABSOLUTE = re.compile(r"(?i)\b(?:no\s+parallel|no\s+equivalent|unprecedented|never\s+before|"
                       r"the\s+only|the\s+first|nothing\s+like|no\s+other)\b")
_SCOPED = re.compile(r"(?i)\b(?:no\s+parallel|no\s+equivalent|the\s+only|the\s+first|no\s+other)\b"
                     r"[^.!?]{0,60}?\b(?:in|among|within|of)\s+(?:the\s+)?[A-Za-z]")
_CAUSAL = re.compile(r"(?i)\b(?:caused|amplified|drove|asphyxiat\w+|emptied|determined|produced|"
                     r"triggered|doomed|guaranteed|ensured)\b")
_HEDGE_NEAR = re.compile(r"(?i)\b(?:likely|probably|perhaps|may|might|appears?|suggests?|"
                         r"evidence|scholars?|historians?|arguably|in\s+part)\b")
_SPEECH = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:wrote|said|recalled|asked|noted|"
                     r"recorded|declared|reported)\s+that\b")
_RELATION = re.compile(r"(?i)\b(?:his|her|their)\s+(?:brother|nephew|uncle|cousin|son|daughter|father|"
                       r"mother|wife|husband|heir|successor)\b|\bdrafted\s+by\b|\bauthored\s+by\b")
_GLOSS = re.compile(r"(?i)(?:\bliterally\b|\bmeans\b|['‘“][^'’”]{2,40}['’”]\s*[—-]|the\s+word\s+for)")

# FG-SEARCH Phase-1 Class-1: dedicated `date` extraction. A DATE is a bare year, a
# spelled year (ID), a "day month year", or a "month year" — verbatim tokens are the
# verifiable unit for R-FG8. Bilingual month lists (ID + EN).
_MONTH_RX = (r"(?:jan(?:uari)?|feb(?:ruari)?|mar(?:et|ch)?|apr(?:il)?|mei|may|jun[ie]?|"
             r"jul[iy]?|agustus|aug(?:ust)?|sep(?:tember)?|okt(?:ober)?|oct(?:ober)?|"
             r"nov(?:ember)?|des(?:ember)?|dec(?:ember)?)")
_DATE_EXTRACT_RX = re.compile(
    r"(?i)"
    r"\b(\d{1,2}\s+" + _MONTH_RX + r"\s+\d{3,4})\b"
    r"|\b(" + _MONTH_RX + r"\s+\d{3,4})\b"
    r"|\b(1\d{3}|20\d{2})\b"
    r"|\b((?:seribu|seratus)(?:\s+(?:satu|dua|tiga|empat|lima|enam|tujuh|delapan|"
    r"sembilan|puluh|belas|ratus|ribu))+)\b")

# FG-SEARCH Phase-1 Class-2: proper-noun in INSTITUTIONAL position. Detects capitalized
# multi-word phrases in slots where an institution/office/policy would sit ("Kas Kasasi"
# in Bab 7). Whitelist of pronouns/function words that must NOT anchor a match.
_INSTITUTION_NOUNS = (r"(?:[Kk]as|[Dd]ewan|[Kk]antor|[Kk]omisi|[Ll]embaga|[Bb]adan|"
                      r"[Mm]ajelis|[Bb]enteng|[Ss]istem|[Pp]erjanjian|[Uu]ndang|"
                      r"[Tt]raktat|[Rr]esolusi|[Aa]turan|[Kk]eputusan)")
_INSTITUTION_RX = re.compile(
    r"\b(" + _INSTITUTION_NOUNS + r"\s+[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)\b")

# SPEC v1 §3.1 troop/crowd row (Aceh review): "Kuta Reh 313 total" where 313 is the
# SUBTOTAL of adult men (Kempees). Detect claims of the form <NUMBER> <TOTALIZER> where
# the sentence ALSO carries "sebagian besar bukan …" — the auto-undercut signature. Not
# blocking; a signal for the verify pass to check scope specifically.
_SUBTOTAL_TRAP_RX = re.compile(
    r"(?i)\b(\d{2,5})\s+(?:orang|jiwa|penduduk|tewas|korban)[^.\n]{0,60}?"
    r"(?:sebagian\s+besar|kebanyakan|mayoritas)\s+(?:bukan|tidak)")

# SPEC v1 §3.1 modifier: "sequence claims" (X as response to Y, X after Y) are verify-
# eligible — causal-order inversions (Concentratie-stelsel-as-response-to-Umar class,
# mosque-burning-after-second-expedition class) are contradicted, not style notes.
_SEQUENCE_RX = re.compile(
    r"(?i)\b(?:sebagai\s+respons(?:i|)?\s+atas|setelah|sesudah|menyusul|"
    r"as\s+a\s+response\s+to|in\s+response\s+to|after|following)\b"
    r"[^.\n]{0,80}?(?:\b(?:membangun|meluncurkan|mendirikan|memerintahkan|"
    r"mengeluarkan|menandatangani|built|launched|ordered|signed)\b)")
_FOREIGN_NEAR = re.compile(r"\*[^*]{2,30}\*|\b[a-z]+tl\b|\b[A-Z][a-z]+ah\b")
_SUPERL_QUAL = re.compile(r"(?i)\b(?:most\s+detailed|greatest|finest|unmatched|unrivalled|unrivaled|"
                          r"most\s+sophisticated|most\s+advanced)\b")
_AMONG = re.compile(r"(?i)\bamong\s+the\b|\bone\s+of\s+the\b")

# Per-JOB (ContextVar), not a module global: known_good is loaded per project at job
# start and read during that job's scan. Many jobs share one process (worker
# concurrency=4 / 128-thread executor), so a global would let job B's set_known_good()
# exempt job A's real errors from the fact scan (cross-project bleed). ContextVar isolates
# each job — set before the gen task spawns; to_thread copies the context.
_KNOWN_GOOD: contextvars.ContextVar[list] = contextvars.ContextVar("narasi_known_good", default=[])


def set_known_good(rows: list[dict[str, Any]]) -> None:
    """Load known_good_claims patterns (per-project + global) into THIS job's context —
    matched sentences are exempt from the sweep (cache/noise damping; the verify pass
    writes these)."""
    pats = []
    for r in rows or []:
        p = (r.get("claim_pattern") or "").strip()
        if not p:
            continue
        try:
            pats.append(re.compile(p))
        except Exception:  # noqa: BLE001
            continue
    _KNOWN_GOOD.set(pats)


def _known_good(s: str) -> bool:
    return any(p.search(s) for p in _KNOWN_GOOD.get())


def fact_scan(text: str, *, factual_regime: str = "strict", lang: str = "en") -> dict:
    """Deterministic scan → fact_report. Report-only (no blocking, no LLM, no search).
    fictional regime: external-claim classes are skipped (only the numeric sweep runs,
    labeled canon-scope). Never raises.

    lang: enables the language pack's spelled-quantity detector — ID prose SPELLS its
    numbers ("dua ratus prajurit", "tiga puluh ribu kilometer persegi"), so a digits-only
    sweep is blind on the ID path (the Diponegoro run's invented statistics all passed)."""
    # 'fiction' (registry-P1 spelling: kdrama_serial/romance_contemporary/remaja_coming_of_age)
    # ≡ 'fictional' — the skip gate below tests == "fictional", so the raw value ran the
    # full external-claim scan on fiction manuscripts. Defense-in-depth; primary
    # normalization lives in narration_api._effective_regime.
    if str(factual_regime or "").strip().lower() == "fiction":
        factual_regime = "fictional"
    rep: dict[str, Any] = {"regime": factual_regime, "classes": {}, "mode": "scan-and-report",
                           "note": "pattern-swept, not guaranteed (R-FG10 §5); verification "
                                   "protocols pending search infra"}
    _spelled_qty = None
    try:
        from narasi_counters import LANGUAGE_PACKS as _LP
        _spelled_qty = (_LP.get((lang or "en").split("-")[0].lower()) or {}).get("spelled_quantity")
    except Exception:  # noqa: BLE001
        _spelled_qty = None
    try:
        # drop heading/header LINES first (else the first prose sentence glues to the
        # preceding "## ..." heading and gets filtered out with it), then sentence-split
        body_text = "\n".join(ln for ln in (text or "").split("\n")
                              if not ln.strip().startswith(("#", ">")))
        body_sents = [s for s in _SENT.split(body_text) if s.strip()]

        def _collect(name, hits):
            rep["classes"][name] = {"count": len(hits), "samples": [h[:180] for h in hits[:8]]}

        # ── R-FG9 numeric sweep ──
        numeric = []
        for s in body_sents:
            if _known_good(s) or _IDIOMS.search(s) or _CONDITIONAL.search(s):
                continue
            toks = [m.group(0) for m in _DIGITS.finditer(s) if any(ch.isdigit() for ch in m.group(0))]
            toks += [m.group(0) for m in _SPELLED.finditer(s)]
            if _spelled_qty is not None:
                toks += [m.group(0) for m in _spelled_qty.finditer(s)]
            # bare years are reported separately (dates class — binary, low-noise)
            toks = [t for t in toks if not _YEAR.fullmatch(t.strip())]
            if toks:
                numeric.append(s)
        _collect("quantitative_unverified", numeric)
        years = [s for s in body_sents if _YEAR.search(s) and not _known_good(s)]
        _collect("date_tokens", years)

        # FG-SEARCH Phase-1: emit `date` and `proper_noun` as separately-verifiable
        # classes so the verify pass can apply per-class policy (date: exact/hedge-prose/
        # cut, never wrong-value; proper_noun: verified/fabricated/wrong-referent).
        # tokens carry the SENTENCE (context for the verdict) + the token itself.
        date_hits = []
        for s in body_sents:
            if _known_good(s):
                continue
            for m in _DATE_EXTRACT_RX.finditer(s):
                tok = next((g for g in m.groups() if g), "").strip()
                if tok:
                    date_hits.append({"sentence": s[:200], "token": tok})
        rep["classes"]["date"] = {"count": len(date_hits),
                                  "samples": [h["sentence"] for h in date_hits[:8]],
                                  "tokens": date_hits[:80]}

        inst_hits = []
        for s in body_sents:
            if _known_good(s):
                continue
            for m in _INSTITUTION_RX.finditer(s):
                inst_hits.append({"sentence": s[:200], "token": m.group(1)})
        rep["classes"]["proper_noun_institution"] = {
            "count": len(inst_hits),
            "samples": [h["sentence"] for h in inst_hits[:8]],
            "tokens": inst_hits[:40],
        }

        # SPEC v1 §3.1 troop/crowd — subtotal-vs-total trap (Aceh: Kuta Reh 313)
        subtotal = []
        for s in body_sents:
            if _known_good(s):
                continue
            m = _SUBTOTAL_TRAP_RX.search(s)
            if m:
                subtotal.append({"sentence": s[:200], "token": m.group(1),
                                 "note": "figure may be a subtotal (adult men); verify against full breakdown"})
        rep["classes"]["subtotal_scope"] = {
            "count": len(subtotal),
            "samples": [h["sentence"] for h in subtotal[:6]],
            "tokens": subtotal[:20],
        }

        # SPEC v1 §3.1 modifier — sequence claims (causal-order verify-eligible)
        sequence = []
        for s in body_sents:
            if _known_good(s):
                continue
            if _SEQUENCE_RX.search(s):
                sequence.append({"sentence": s[:200], "token": ""})
        rep["classes"]["sequence"] = {
            "count": len(sequence),
            "samples": [h["sentence"] for h in sequence[:8]],
            "tokens": sequence[:30],
        }

        if factual_regime == "fictional":
            rep["skipped"] = "external-claim classes skipped (fictional regime; FG2b continuity applies elsewhere)"
            if "quantitative_unverified" in rep["classes"]:
                rep["classes"]["quantitative_unverified"]["scope"] = "canon"
            return rep

        # ── R-FG10 wave 1 ──
        absolutes = []
        for s in body_sents:
            if not _ABSOLUTE.search(s) or _known_good(s):
                continue
            if _SCOPED.search(s):        # correctly scoped absolute → PASS (negative control)
                continue
            if _CONDITIONAL.search(s) or _UNKNOWABLE.search(s):
                continue
            absolutes.append(s)
        _collect("negative_existence_unscoped", absolutes)

        causal = []
        for i, s in enumerate(body_sents):
            if not _CAUSAL.search(s) or _known_good(s):
                continue
            window = " ".join(body_sents[max(0, i - 1):i + 2])
            if _HEDGE_NEAR.search(window):   # hedged or attributed nearby → exempt
                continue
            causal.append(s)
        _collect("causal_mechanism_unhedged", causal)

        # ── wave 2 ──
        _collect("attributed_quote", [s for s in body_sents if _SPEECH.search(s) and not _known_good(s)])
        _collect("proper_relation", [s for s in body_sents if _RELATION.search(s) and not _known_good(s)])

        # ── wave 3 ──
        glosses = [s for s in body_sents
                   if _GLOSS.search(s) and _FOREIGN_NEAR.search(s) and not _known_good(s)]
        _collect("etymology_gloss", glosses)
        superls = [s for s in body_sents
                   if _SUPERL_QUAL.search(s) and not _AMONG.search(s)
                   and not any(ch.isdigit() for ch in s) and not _known_good(s)]
        _collect("superlative_qualitative", superls)

        rep["deferred"] = ["anachronism_term (needs model-tier period-check)",
                          "R-FG8 verify protocols (needs search infra)"]
        # over-fire tripwire (§4): a class extracting 30+ on one manuscript = detector too hot
        rep["overfiring"] = [k for k, v in rep["classes"].items() if v["count"] >= 30]
        return rep
    except Exception:  # noqa: BLE001
        rep["error"] = "scan_failed"
        return rep
