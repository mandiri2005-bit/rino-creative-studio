"""pakem — the ONE source of truth for narration (Project Dalang).

The "pakem" is the canon: every narration style, the shared assets, and the
single style/language resolvers. It used to be duplicated across
python/laozhang_api.py (STYLE_RULES, get_style_rules, _NARASI_LANG_NAMES,
VIDEO_SCRIPT_MODIFIER), backend/server.js (NARASI_STYLE_RULES_JS), and
data/moat/gutenberg/{style_rag_config,rag_narration}.py. This package unifies
them.

Pure data + functions. No network, no DB, no app imports — safe to import
anywhere (Python /narasi runtime, an editor pass, an eval harness, or a thin
endpoint that serves rules to the Node Google path).

Key concepts:
  - Each style stores BOTH `style_rules_core` (load-bearing rules SHIPPED at
    generation) and `style_rules_editor` (long-form rules used ONLY in an
    editor pass — never shipped at generation).
  - Shared assets (FACTUAL_INTEGRITY, LANGUAGE_DIRECTIVE, GENERATION_PREAMBLE,
    CRAFT_RULES, VIDEO_MODIFIER) are defined ONCE in pakem.assets.
  - ONE style resolver and ONE language resolver in pakem.resolvers.

Usage:
    from pakem import resolve_style, resolve_language, build_style_block
    entry = resolve_style("creative non-fiction documentary")
    rules = build_style_block(entry, video_mode=True)  # core (+ video) — never editor
"""
from __future__ import annotations

import os as _os

from .assets import (
    CRAFT_RULES,
    DRAMA_MANDATES,
    FACTUAL_INTEGRITY,
    GENERATION_PREAMBLE,
    LANGUAGE_DIRECTIVE,
    VIDEO_MODIFIER,
    VIDEO_RULE5_CAPPED,
    VIDEO_RULE5_LEGACY,
)
from .registry import DEFAULT_STYLE, STYLES
from .resolvers import (
    DEFAULT_LANGUAGE_LABEL,
    LANGUAGE_NAMES,
    resolve_language,
    resolve_style,
    resolve_style_key,
)

# Bump on any change to style rules, assets, or resolver behaviour so callers
# (caches, eval baselines) can invalidate. MAJOR.MINOR.PATCH.
PAKEM_VERSION = "2.0.0"   # schema v2: category + factual_regime + style_spec (refactor doc §7.6)

# ── Dual-path medium layer (CC dual-path doc §2/§3) — transform blocks injected at
# GENERATION (option b: the draft is born near-target; no whole-text rewrite pass).
# They run ONLY on medium mismatch; native-match jobs never see them (protects voice).
R_VO = """
MEDIUM ADAPTATION — THIS STYLE WAS BORN ON THE PAGE, YOU ARE WRITING FOR THE EAR (R-VO):
- Breath units: average 8-15 words per sentence, hard max ~25. Split long sentences without mercy.
- Front-load subject-verb; no deep center-embedding; long parentheticals become their own sentence.
- Typography dies: no semicolons (use periods), no footnote asides (speak them), written lists become spoken enumeration ("Three things. First—").
- Numbers speakable and rounded: "around two hundred thousand", never "217,432". Dates spoken: "June thirtieth, fifteen twenty".
- Attribution BEFORE the quote ("Diaz recorded that...") — listeners cannot scroll back.
- Re-anchor names/pronouns every ~3 sentences in multi-actor scenes; lower name density overall.
- Repetition is a FEATURE here (audible signposting); make transitions audible ("But here's where it gets strange—").
- TTS hygiene: expand acronyms on first use; avoid homograph ambiguity; no digit-heavy dates.
The medium tax is real: some page elegance dies in the split. Pay it — clarity for the ear wins.
"""

R_PAGE = """
MEDIUM ADAPTATION — THIS STYLE WAS BORN FOR THE EAR, YOU ARE WRITING A BOOK PAGE (R-PAGE):
- De-signpost: strip verbal tics ("here's the thing", "but wait", "now—") — on the page they read as filler.
- Restore subordination: merge choppy breath-unit sentences where rhythm allows; the eye handles complexity the ear can't.
- Repetition cap TIGHTENED: audible signposting is a bug on the page. Vary instead.
- Reduce rhetorical questions and direct address "you" (unless the style REQUIRES them).
- Precision restored: exact figures preferred where verified ("217,432 registered"); hedge only what is genuinely uncertain.
- Typography allowed: semicolons, parentheticals; vary paragraph length for the eye.
"""

# ── Universal claim discipline (CC v3 / backend-rules #4-5) — appended to EVERY
# style's generation block (Rino 2026-07-04: "semua"). Style-agnostic factual
# hygiene; style-specific craft (zoom, agency, endings) stays in the per-style text.
CLAIM_DISCIPLINE = """
CLAIM DISCIPLINE (applies to every factual statement):
- CERTAIN -> state directly. PROBABLE -> "likely" / "appears" / "the evidence suggests".
- DISPUTED -> name the disagreement in one clause. UNCERTAIN -> soften or cut. Never invent precision.
- No anachronistic frame-terms (e.g. "gold-standard economy" for a pre-modern state -> "bullion-hungry economy"). Modern explanatory terms only when they clarify, never when they decorate.
- A [VERIFY: ...] flag MUST carry your best-estimate value INSIDE the bracket ("[VERIFY: 3-5 km]") — never an empty or generic flag; a flag asks to CHECK a value, it is not a placeholder for one.
- Write ENTIRELY in the output language: never drop standalone English placeholder/hedge words (several, around, roughly, some, approximately) into non-English prose.
- [ANCHOR] lines: MAXIMUM 3 per manuscript. Each must speak in the narration's own POV or carry explicit attribution — an unattributed first-person anchor inside third-person narration reads as invented testimony.
"""


# ── Epistemic render rules per factual_regime (R-FG8 §1 render column + R-FG10 §1),
# enforced at GENERATION so the scan-and-report pass finds little to flag. ──
_REGIME_BLOCKS = {
    "strict": """
FACTUAL RENDERING (strict):
- A period figure that exists only inside a chronicle is a SOURCE-CLAIM: frame it as one ("according to Cortes's own letter...") — never in the narrator's bare voice.
- A contested modern reconstruction (populations, casualties) is a SCHOLARLY-ESTIMATE: ALWAYS a range with estimate-language ("estimates place..."), never a bare number.
- Measurable facts (distances, altitudes, dates): exact or "roughly X" — max ONE hedge-word per figure; a claim needing more hedging gets cut.
- NEVER ship an unscoped absolute ("no parallel", "the only", "unprecedented"): scope it ("no parallel in the Iberian experience") or soften ("few if any").
- An unhedged causal claim must be mechanical necessity; contested mechanisms get ONE of: attribution, "likely", or downgrade to observed correlation.
- Superlatives without numbers only when scholarship itself uses them; otherwise "among the most...".
""",
    "hybrid": """
FACTUAL RENDERING (hybrid — invented narrative on real-world anchors):
- Real places, institutions, events, dates, period technology = ANCHORS: treat them with strict-regime care (accurate, hedged where uncertain).
- Invented characters/events are yours — but their attributes may NEVER drift (eye color, rank, name spelling stay fixed across chapters).
- Never attribute invented actions to real named historical figures.
""",
    "fictional": """
FACTUAL RENDERING (fictional):
- Continuity is the fact-check: established character/world facts may never contradict earlier chapters.
- Keep the world's internal rules stable once stated.
""",
}


def build_style_block(style, video_mode: bool = False) -> str:
    """Return the GENERATION-time style block for a style.

    This is the pakem replacement for laozhang_api.get_style_rules():
      core rules + universal CLAIM_DISCIPLINE (+ VIDEO_MODIFIER when video_mode) —
      and NEVER the editor block.

    `style` may be a raw user string OR an already-resolved entry dict.
    """
    entry = style if isinstance(style, dict) else resolve_style(style)
    rules = entry.get("style_rules_core", "")
    rules = rules.rstrip() + "\n" + CLAIM_DISCIPLINE
    _regime = entry.get("factual_regime", "strict")
    # P1 fiction styles (kdrama_serial/romance_contemporary/remaja_coming_of_age) declare
    # 'fiction'; _REGIME_BLOCKS keys on 'fictional', so they silently fell back to the
    # STRICT factual-rendering block at generation. Normalizing changes generation
    # prompts, so it is flag-gated: NARASI_REGIME_BLOCK_NORMALIZE=1 to enable.
    if _regime == "fiction" and str(_os.environ.get("NARASI_REGIME_BLOCK_NORMALIZE", "0")).strip().lower() in ("1", "true", "yes", "on"):
        _regime = "fictional"
    rules = rules.rstrip() + "\n" + _REGIME_BLOCKS.get(_regime, _REGIME_BLOCKS["strict"])
    # Dramatization mandates (NARASI_DRAMA_MANDATES, default OFF) — round-3 craft:
    # keystone decisions rendered on-page + narrative promises paid on-page. Fiction
    # regimes only (a documentary summarizing an outcome is normal nonfiction craft).
    if (_regime in ("fiction", "fictional")
            and str(_os.environ.get("NARASI_DRAMA_MANDATES", "0")).strip().lower() in ("1", "true", "yes", "on")):
        rules = rules.rstrip() + "\n\n" + DRAMA_MANDATES
    # LANE NEGATIVE — CHAPTER LEVEL (NARASI_LANE_LEDGER_CHAPTER, default OFF, round-4).
    # Roll-6 terminal proof of Law 1: the lane ledger reached only the BIBLE prompt, so
    # chapter writers freely re-improvised banned specifics (seven 'eleven's, clocks 6:14
    # and 11:40 verbatim from the ban list, ramyeon two rolls running) — the banned motif
    # even bent a plot fact (Ru-an 4th → 11th on the same list). One COMPACT negative
    # line (clocks + numbers + foods — the per-chapter improvisable classes) in every
    # chapter prompt closes the loop. Names/places stay bible-level: the bible pins those
    # and chapters inherit them. Fail-open: missing file/lane ⟹ rules unchanged.
    if (_regime in ("fiction", "fictional")
            and str(_os.environ.get("NARASI_LANE_LEDGER_CHAPTER", "0")).strip().lower() in ("1", "true", "yes", "on")):
        try:
            import json as _lj
            import re as _lre
            _lkey = entry.get("key") or ""
            _lpath = _os.path.join(_os.path.dirname(__file__), "lane_ledger.json")
            with open(_lpath, encoding="utf-8") as _lf:
                _lane = (_lj.load(_lf) or {}).get(_lkey) or {}
            if isinstance(_lane, dict) and _lane:
                def _lt(x):
                    return _lre.sub(r"\s*\([^)]*\)\s*$", "", str(x)).strip().lstrip("…").strip()
                _nums = [str(x) if isinstance(x, int) else _lt(x)
                         for x in (_lane.get("small_numbers") or [])][:10]
                _ts = [_lt(x) for x in (_lane.get("timestamp_minutes") or [])][:6]
                _fds = [_lt(x) for x in (_lane.get("foods") or [])][:8]
                _phr = [_lt(x) for x in (_lane.get("verbatim_phrases") or [])
                        if len(_lt(x)) >= 12][:8]
                if _nums or _ts or _fds or _phr:
                    rules = rules.rstrip() + (
                        "\n\nLANE NEGATIVE CONSTRAINTS (compact): previous stories in this exact "
                        "lane overused the following. NEVER use them for any number, time, or dish "
                        "YOU invent in this chapter (values the premise or the fact sheet itself "
                        "pins are exempt — obey the sheet)."
                        + ((" Clock minutes to avoid: " + ", ".join(_ts) + ".") if _ts else "")
                        + ((" Numbers to avoid, digits or spelled: " + ", ".join(_nums) + ".") if _nums else "")
                        + ((" Comfort foods to avoid: " + ", ".join(_fds) + ".") if _fds else "")
                        + ((" NEVER reproduce or near-vary these sentences/frames: \"" +
                            "\"; \"".join(_phr) + "\".") if _phr else ""))
        except Exception:  # noqa: BLE001 — ledger is an enhancement, never blocks a chapter
            pass
    # Dual-path selector (dual-path doc §1): compare the style's NATIVE medium with the
    # job's output target. Native match → light normalization only (VIDEO_MODIFIER on the
    # video path, nothing extra on book). Mismatch → the aggressive transform block.
    origin = entry.get("medium_origin", "page")
    if video_mode:
        rules = rules.rstrip() + "\n" + VIDEO_MODIFIER
        # Anchor hard budget (NARASI_ANCHOR_BUDGET, default OFF): swap legacy RULE 5
        # (3–5 [ANCHOR]/chapter) for the capped variant (≤1/chapter, epigraph position,
        # most chapters none). Round-2 craft: the request site was training the lane
        # into gnomic mode (7→11→20 floating aphorisms across 3 kdrama prod rolls).
        if str(_os.environ.get("NARASI_ANCHOR_BUDGET", "0")).strip().lower() in ("1", "true", "yes", "on"):
            rules = rules.replace(VIDEO_RULE5_LEGACY, VIDEO_RULE5_CAPPED)
        if origin == "page":
            rules = rules.rstrip() + "\n" + R_VO          # page→ear: aggressive adaptation
    else:
        if origin == "ear":
            rules = rules.rstrip() + "\n" + R_PAGE        # ear→page: de-signpost for the eye
    return rules


def get_editor_block(style) -> str:
    """Return the EDITOR-pass style block (long-form). NEVER ship at generation.

    Empty string for styles that have no separate editor layer.
    """
    entry = style if isinstance(style, dict) else resolve_style(style)
    return entry.get("style_rules_editor", "") or ""


__all__ = [
    "PAKEM_VERSION",
    # registry
    "STYLES",
    "DEFAULT_STYLE",
    # resolvers
    "resolve_style",
    "resolve_style_key",
    "resolve_language",
    "LANGUAGE_NAMES",
    "DEFAULT_LANGUAGE_LABEL",
    # assets
    "FACTUAL_INTEGRITY",
    "CRAFT_RULES",
    "LANGUAGE_DIRECTIVE",
    "GENERATION_PREAMBLE",
    "VIDEO_MODIFIER",
    # helpers
    "build_style_block",
    "get_editor_block",
]
