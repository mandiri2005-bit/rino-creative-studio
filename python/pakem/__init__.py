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
PAKEM_VERSION = "1.0.0"


def build_style_block(style, video_mode: bool = False) -> str:
    """Return the GENERATION-time style block for a style.

    This is the pakem replacement for laozhang_api.get_style_rules():
      core rules (+ VIDEO_MODIFIER when video_mode) — and NEVER the editor block.

    `style` may be a raw user string OR an already-resolved entry dict.
    """
    entry = style if isinstance(style, dict) else resolve_style(style)
    rules = entry.get("style_rules_core", "")
    if video_mode:
        rules = rules.rstrip() + "\n" + VIDEO_MODIFIER
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
