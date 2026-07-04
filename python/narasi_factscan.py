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


def fact_scan(text: str, *, factual_regime: str = "strict") -> dict:
    """Deterministic scan → fact_report. Report-only (no blocking, no LLM, no search).
    fictional regime: external-claim classes are skipped (only the numeric sweep runs,
    labeled canon-scope). Never raises."""
    rep: dict[str, Any] = {"regime": factual_regime, "classes": {}, "mode": "scan-and-report",
                           "note": "pattern-swept, not guaranteed (R-FG10 §5); verification "
                                   "protocols pending search infra"}
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
            # bare years are reported separately (dates class — binary, low-noise)
            toks = [t for t in toks if not _YEAR.fullmatch(t.strip())]
            if toks:
                numeric.append(s)
        _collect("quantitative_unverified", numeric)
        years = [s for s in body_sents if _YEAR.search(s) and not _known_good(s)]
        _collect("date_tokens", years)

        if factual_regime == "fictional":
            rep["skipped"] = "external-claim classes skipped (fictional regime; FG2b continuity applies elsewhere)"
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
