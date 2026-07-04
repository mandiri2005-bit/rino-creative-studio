# ── narasi_contested — SPEC v1 §3.10/§12.2 contested_historiography flag.
#
# When ON (per project or per style_spec), tightens the fact-gate posture on the
# scholarly-estimate class:
#
#   1. Casualty/atrocity numbers from partisan sources (Parlindungan class) are
#      quarantined to `scholarly-estimate` — MUST be rendered as range + attribution,
#      NEVER as bare fact.
#   2. Framing evenhandedness is itself a review checkpoint — the manuscript should not
#      lean heavily on one side of a live historiographical debate.
#   3. This is a FACT-GATE POSTURE, not a content-safety block. The topic is legitimate;
#      the discipline is on sourcing and framing.
#
# Usage:
#   from narasi_contested import is_contested, posture_for, scan_partisan_bareclaims
#   if is_contested(project="bonjol"): ...
from __future__ import annotations

import re
from typing import Any

__all__ = ["is_contested", "posture_for", "scan_partisan_bareclaims",
           "CONTESTED_PROJECTS", "PARTISAN_SOURCES"]

# ── project-level configuration. Seeded from the corpus (Bonjol + Aceh flagged after
# their reviews); grows per review. Also settable per job via style_spec.contested=True.
CONTESTED_PROJECTS: dict[str, dict[str, Any]] = {
    "bonjol": {
        "reason": "Perang Padri — Tuanku Rao debate, Parlindungan vs Hamka, "
                  "atrocity-attribution among Muslims",
        "estimate_tighten": True,
        "framing_checkpoint": True,
    },
    "aceh": {
        "reason": "Perang Aceh — Dutch-colonial casualty accounting is contested; "
                  "Snouck-Hurgronje/van Daalen atrocity narrative varies by author",
        "estimate_tighten": True,
        "framing_checkpoint": True,
    },
    "tuanku-rao": {
        "reason": "Direct Parlindungan-Hamka locus; every estimate is a live dispute",
        "estimate_tighten": True,
        "framing_checkpoint": True,
    },
}

# Partisan sources known to inflate casualties or push a specific narrative — when a
# manuscript names one of these as the source of an estimate, treat the estimate as
# HEDGED-ONLY (never bare number).
PARTISAN_SOURCES = {
    "parlindungan",       # Tuanku Rao (1964) — highly inflated Batak casualties
    "mangaraja onggang parlindungan",
    "hamka",              # Ayahku / responses to Parlindungan — different partisan lean
    "buya hamka",
    # Colonial-era Dutch counts of Aceh war "official" figures — quarantined per Aceh review
    "kolonial belanda",  # generic; specific colonial sources may pass
    "angka resmi kolonial",
}

_ESTIMATE_UNITS = (r"(?:orang|jiwa|korban|tewas|meninggal|penduduk|"
                    r"deaths?|casualt(?:ies|y)|killed|people|inhabitants|"
                    r"morts?|victimes|tu[eé]s|habitants)")
# Big round numbers that would be uncomfortable as bare fact in a contested context.
# Grow the list of high-round-number shapes rather than trying to detect all numerics.
_BIG_ROUND_RX = re.compile(
    r"(?<![\d.,])(?P<num>(?:1\d|2\d|3\d|4\d|5\d|6\d|7\d|8\d|9\d)[.,]?000|"
    r"1[.,]?00[.,]?000|2[.,]?00[.,]?000|300\.000|400\.000|500\.000|"
    r"1\s+juta|1\s+million)(?![\d])"
)
# Range/hedge markers — any of these near the number = properly hedged, not a bareclaim.
_HEDGE_MARKERS = re.compile(
    r"(?i)(?:sekitar|kurang\s+lebih|kira-kira|hingga|sampai|antara|"
    r"between|around|roughly|approximately|about|nearly|up\s+to|"
    r"environ|jusqu[’']?[àa]|entre|approximativement|"
    r"estimasi|perkiraan|diperkirakan|estimated|estimation)")


def is_contested(project: str | None = None, style_spec: dict | None = None,
                 job_flag: bool | None = None) -> bool:
    """Resolve the contested flag from any of: explicit job flag, style_spec, or project
    config. Precedence: job_flag → style_spec → project."""
    if job_flag is not None:
        return bool(job_flag)
    if style_spec and style_spec.get("contested_historiography"):
        return True
    if project and project.lower() in CONTESTED_PROJECTS:
        return True
    return False


def posture_for(project: str | None = None, style_spec: dict | None = None,
                 job_flag: bool | None = None) -> dict[str, Any]:
    """Return the active posture dict. Callers read `estimate_tighten` and
    `framing_checkpoint` and adjust their own behaviour."""
    if not is_contested(project=project, style_spec=style_spec, job_flag=job_flag):
        return {"active": False, "estimate_tighten": False, "framing_checkpoint": False}
    cfg = CONTESTED_PROJECTS.get((project or "").lower(), {})
    return {
        "active": True,
        "reason": cfg.get("reason", "flagged via style_spec or job"),
        "estimate_tighten": cfg.get("estimate_tighten", True),
        "framing_checkpoint": cfg.get("framing_checkpoint", True),
    }


def scan_partisan_bareclaims(text: str, *, project: str | None = None,
                              style_spec: dict | None = None,
                              job_flag: bool | None = None) -> list[dict]:
    """Detect casualty/atrocity numbers that are:
       (a) big round (≥10k), (b) NOT hedged, and (c) EITHER attributed to a partisan
           source or appearing in a manuscript on a contested project.
    Returns a findings list; empty if contested-posture is inactive or text is clean.
    """
    if not text:
        return []
    posture = posture_for(project=project, style_spec=style_spec, job_flag=job_flag)
    if not posture["active"] or not posture["estimate_tighten"]:
        return []

    findings: list[dict] = []
    for m in _BIG_ROUND_RX.finditer(text):
        # Bounded to same sentence to avoid cross-paragraph noise.
        lo = m.start()
        while lo > 0 and text[lo - 1] not in ".!?\n":
            lo -= 1
        hi = m.end()
        while hi < len(text) and text[hi] not in ".!?\n":
            hi += 1
        sentence = text[lo:hi]

        # Is it a casualty/estimate class number?
        if not re.search(_ESTIMATE_UNITS, sentence, re.IGNORECASE):
            continue

        hedge = bool(_HEDGE_MARKERS.search(sentence))
        # Partisan-source attribution must be in the SAME sentence — prior wider window
        # attached the wrong scholar to the wrong number when they appeared adjacent.
        window = sentence.lower()
        partisan_hit = None
        for src in PARTISAN_SOURCES:
            if src in window:
                partisan_hit = src
                break

        if partisan_hit and not hedge:
            findings.append({
                "kind": "partisan_bareclaim",
                "number": m.group("num"),
                "partisan_source": partisan_hit,
                "sentence": sentence.strip()[:220],
                "policy": "quarantine_to_scholarly_estimate",
                "note": f"partisan-source estimate must render as range + attribution",
            })
        elif not hedge:
            # Contested project + bare big number + no hedge = tighten
            findings.append({
                "kind": "contested_bareclaim",
                "number": m.group("num"),
                "sentence": sentence.strip()[:220],
                "policy": "add_hedge_or_attribution",
                "note": "contested-project big-round casualty: hedge or attribute or cut",
            })
    return findings
