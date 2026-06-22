# video/visualPrompt/schema.py — JSON-schema + Python validator for LLM-generated
# non-WB visual prompt output.  Mirrors the behaviour of the Node validate.mjs:
#   • raises nothing — returns (ok, errors, warnings) tuple
#   • Chastelein-bug check: proper nouns in visual_prompt must be a subset of
#     those in narration_text (+ setting field)
#   • minimum 200 chars, maximum 600 chars on visual_prompt
#   • characters entries must appear in narration_text
#
# The jsonschema dict (VISUAL_PROMPT_OUTPUT_SCHEMA) can be used with any
# JSON-Schema validator (e.g. `jsonschema.validate(data, VISUAL_PROMPT_OUTPUT_SCHEMA)`).

from __future__ import annotations

import re
from typing import Any

# ── JSON Schema (draft-07 compatible) ────────────────────────────────────────

VISUAL_PROMPT_OUTPUT_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "VisualPromptOutput",
    "description": (
        "LLM-generated visual prompt object for a non-WB video scene. "
        "visual_prompt goes directly to the image/clip model; characters/setting/mood "
        "are optional enrichment fields used by downstream analytics + the Chastelein validator."
    ),
    "type": "object",
    "required": ["visual_prompt"],
    "additionalProperties": False,
    "properties": {
        "visual_prompt": {
            "type": "string",
            "minLength": 200,
            "maxLength": 600,
            "description": (
                "Cinematography prompt for the image/clip model. Must be "
                "200-600 characters.  Must NOT contain proper nouns absent from "
                "the scene's narration_text (Chastelein check)."
            ),
        },
        "characters": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": (
                "Named entities (people, characters) depicted in the scene.  "
                "Each entry MUST appear in narration_text."
            ),
        },
        "setting": {
            "type": "string",
            "description": "Location / environment description (common nouns acceptable).",
        },
        "mood": {
            "type": "string",
            "description": "Emotional tone or atmosphere for this scene.",
        },
    },
}


# ── helpers ───────────────────────────────────────────────────────────────────

_TITLE_WORDS = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "rev", "lord", "lady", "sir", "dame",
    "van", "de", "den", "von", "bin", "binti", "ibn", "st",
})

# Common vocabulary that appears capitalised in image prompts but is NOT a proper
# noun.  Mirrors the COMMON_VOCAB set in validate.mjs.  Extend here when false
# positives appear in the warning logs.
_COMMON_VOCAB = frozenset({
    # English determiners / prepositions / conjunctions
    "the", "a", "an", "in", "on", "at", "by", "for", "with", "as", "and",
    "but", "or", "nor", "yet", "so", "its", "his", "her", "their", "our",
    # Cinematography / scene-description adjectives and common nouns
    "wide", "close", "long", "medium", "extreme", "overhead", "aerial",
    "slow", "fast", "steady", "handheld", "static",
    "warm", "cool", "bright", "dark", "soft", "harsh", "golden", "silver",
    "amber", "blue", "green", "red", "white", "black", "grey", "gray",
    "misty", "foggy", "dusty", "hazy", "lush", "dense", "arid",
    "cinematic", "dramatic", "sweeping", "intimate", "atmospheric",
    "establishing", "tracking", "dolly", "crane", "tilt", "pan",
    "fishing", "farming", "trading", "walking", "running", "standing", "sitting",
    "morning", "afternoon", "evening", "night", "dawn", "dusk", "sunset", "sunrise",
    "market", "village", "city", "town", "river", "forest", "mountain", "field",
    "birds", "trees", "clouds", "waves", "light", "shadow", "mist", "smoke",
    "people", "man", "woman", "child", "figure", "vendor", "soldier", "worker",
    "street", "road", "path", "building", "house", "temple", "palace", "bridge",
    "boats", "ships", "water", "sky", "earth", "land", "sea", "ocean", "hill",
    "fire", "rain", "sun", "moon", "stars", "wind", "dust", "sand",
    "two", "three", "four", "five", "several", "many", "few", "some", "all",
    # Indonesian stopwords / connectives
    "yang", "dan", "di", "ke", "dari", "itu", "ini", "para", "pada",
    "untuk", "dengan", "adalah", "sebuah", "seorang", "akan", "tidak",
    "juga", "atau", "mereka", "kita", "kami", "ada", "setelah", "banyak",
    "terlihat", "tampak", "terbang", "bergerak", "berlari", "berdiri",
    "cahaya", "bayangan", "langit", "tanah", "hutan", "gunung", "sungai",
})

_PROPER_NOUN_RE = re.compile(r'\b[A-ZÀ-ɏ][A-Za-zÀ-ɏ]+', re.UNICODE)


def _extract_proper_nouns(text: str) -> set[str]:
    """Heuristic: words that start with an upper-case letter, len >= 3,
    not a title/honorific or common vocabulary.  Returns a set of lowercased
    tokens.  Mirrors extractProperNouns() in validate.mjs."""
    if not text:
        return set()
    tokens = _PROPER_NOUN_RE.findall(text)
    out: set[str] = set()
    for t in tokens:
        lc = t.lower()
        if len(lc) >= 3 and lc not in _TITLE_WORDS and lc not in _COMMON_VOCAB:
            out.add(lc)
    return out


# ── main validator ────────────────────────────────────────────────────────────

def validate_visual_prompt_output(
    output: Any,
    narration_text: str,
    *,
    scene_id: str = "?",
    min_chars: int = 200,
    max_chars: int = 600,
) -> tuple[bool, list[str], list[str]]:
    """Validate an LLM-generated visual prompt output dict.

    Returns (ok, errors, warnings).  ok=False means the caller MUST fall back
    to build_visual_prompt().

    Parameters
    ----------
    output:         The raw LLM output (any Python value).
    narration_text: The narration spoken over this scene (for Chastelein check).
    scene_id:       Used only in log messages.
    min_chars / max_chars: length bounds on visual_prompt (overridable for tests).
    """
    errors:   list[str] = []
    warnings: list[str] = []
    narration = (narration_text or "").strip()

    # ── 1. shape guard ────────────────────────────────────────────────────────
    if not isinstance(output, dict):
        errors.append("output is not a dict")
        return False, errors, warnings

    # ── 2. visual_prompt: required, str, length range ─────────────────────────
    vp = output.get("visual_prompt")
    if not isinstance(vp, str) or not vp.strip():
        errors.append("visual_prompt is missing or empty")
    else:
        vp_stripped = vp.strip()
        length = len(vp_stripped)
        if length < min_chars:
            errors.append(
                f"visual_prompt too short ({length} chars, min {min_chars}) — lazy generation"
            )
        if length > max_chars:
            # over-long is non-fatal: image models truncate silently anyway
            warnings.append(
                f"visual_prompt too long ({length} chars, max {max_chars}) — trimmed by image model"
            )

    # ── 3. Chastelein check ───────────────────────────────────────────────────
    if isinstance(vp, str) and vp.strip():
        prompt_nouns  = _extract_proper_nouns(vp)
        narrat_nouns  = _extract_proper_nouns(narration)
        setting_nouns = _extract_proper_nouns(output.get("setting") or "")
        universe      = narrat_nouns | setting_nouns
        narrat_lower  = narration.lower()
        # stray = in prompt but NOT in narration as a proper noun AND NOT a substring
        strays = [
            n for n in prompt_nouns
            if n not in universe and n not in narrat_lower
        ]
        if strays:
            errors.append(
                f"visual_prompt contains proper noun(s) not in narration_text: "
                f"{', '.join(strays)} (Chastelein bug — name from brief, not this scene)"
            )

    # ── 4. characters ─────────────────────────────────────────────────────────
    chars = output.get("characters")
    if chars is not None:
        if not isinstance(chars, list):
            warnings.append("characters is not a list — ignored")
        else:
            narrat_lower = narration.lower()
            for entry in chars:
                if not isinstance(entry, str):
                    warnings.append(f"characters: non-string entry {entry!r} — skipped")
                    continue
                name = entry.strip()
                if name and name.lower() not in narrat_lower:
                    errors.append(
                        f"characters entry {entry!r} not found in narration_text — "
                        "character from brief injected into wrong scene"
                    )

    # ── 5. optional string fields ─────────────────────────────────────────────
    for field in ("setting", "mood"):
        val = output.get(field)
        if val is not None and not isinstance(val, str):
            warnings.append(f"{field} is not a string (got {type(val).__name__}) — ignored")

    # ── 6. unknown extra keys ─────────────────────────────────────────────────
    known = {"visual_prompt", "characters", "setting", "mood"}
    extra = set(output.keys()) - known
    if extra:
        warnings.append(f"unknown fields in output: {', '.join(sorted(extra))} — ignored")

    return len(errors) == 0, errors, warnings


# ── fallback helper ───────────────────────────────────────────────────────────

def build_visual_prompt_fallback(
    scene_visual_prompt: str,
    errors: list[str],
    scene_id: str = "?",
) -> str:
    """Return the existing regex-built visual_prompt for this scene and emit
    a warning.  Call when validate_visual_prompt_output returns ok=False.

    Parameters
    ----------
    scene_visual_prompt: the .visual_prompt string already on the Scene object
                         (produced by build_visual_prompt() in video_segmenter.py).
    errors:              error list from validate_visual_prompt_output, for logging.
    """
    import logging
    logging.getLogger(__name__).warning(
        "[visualPrompt %s] LLM output invalid → regex fallback. Errors: %s",
        scene_id, "; ".join(errors),
    )
    return scene_visual_prompt or ""
