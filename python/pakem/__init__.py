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

from .assets import (
    CRAFT_RULES,
    FACTUAL_INTEGRITY,
    GENERATION_PREAMBLE,
    LANGUAGE_DIRECTIVE,
    VIDEO_MODIFIER,
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
PAKEM_VERSION = "1.2.0"

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
"""


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
    # Dual-path selector (dual-path doc §1): compare the style's NATIVE medium with the
    # job's output target. Native match → light normalization only (VIDEO_MODIFIER on the
    # video path, nothing extra on book). Mismatch → the aggressive transform block.
    origin = entry.get("medium_origin", "page")
    if video_mode:
        rules = rules.rstrip() + "\n" + VIDEO_MODIFIER
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
