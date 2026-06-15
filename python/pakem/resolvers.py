"""pakem.resolvers — ONE style resolver + ONE language resolver.

These replace the scattered resolution logic that used to live in:
  - laozhang_api.get_style_rules()  (substring match over STYLE_RULES)
  - laozhang_api._rag_style() + _RAG_STYLE_LEGACY  (alias map)
  - laozhang_api._resolve_narasi_lang() + _NARASI_LANG_NAMES
  - backend/server.js _getStyleRulesJS() / resolveLang()
  - style_rag_config._ALIASES + get_style_config()

Single source of truth. Pure functions, no network.
"""
from __future__ import annotations

from typing import Optional

from .registry import STYLES, DEFAULT_STYLE

# ---------------------------------------------------------------------------
# Language table — the ONE table (from laozhang_api._NARASI_LANG_NAMES /
# backend/server.js _NARASI_LANG_NAMES — identical copies, unified here).
# ---------------------------------------------------------------------------
LANGUAGE_NAMES: dict[str, str] = {
    "id": "Bahasa Indonesia", "en": "English",
    "jv": "Basa Jawa (Javanese)", "su": "Basa Sunda (Sundanese)",
    "ms": "Bahasa Melayu (Malay)", "ban": "Basa Bali (Balinese)",
    "min": "Baso Minangkabau", "ar": "العربية (Arabic)",
    "zh": "中文 (Chinese)", "ja": "日本語 (Japanese)", "ko": "한국어 (Korean)",
    "es": "Español (Spanish)", "fr": "Français (French)", "de": "Deutsch (German)",
    "nl": "Nederlands (Dutch)", "pt": "Português (Portuguese)",
    "hi": "हिन्दी (Hindi)", "th": "ภาษาไทย (Thai)", "vi": "Tiếng Việt (Vietnamese)",
    "tl": "Tagalog (Filipino)",
}

DEFAULT_LANGUAGE_LABEL = "Bahasa Indonesia"


# ---------------------------------------------------------------------------
# Alias index — built once at import from every entry's `aliases`.
# Maps a normalized alias -> canonical style key.
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    return (s or "").strip().lower()


_ALIAS_INDEX: dict[str, str] = {}
for _key, _entry in STYLES.items():
    _ALIAS_INDEX[_norm(_key)] = _key
    for _alias in _entry.get("aliases", ()):
        _ALIAS_INDEX[_norm(_alias)] = _key
del _key, _entry  # type: ignore[has-type]  # keep module namespace clean


def resolve_style_key(style: Optional[str]) -> str:
    """Resolve any user/legacy/JS style string to a canonical registry key.

    Resolution order:
      1. exact normalized alias/key match
      2. substring match (longest alias contained in the input wins) — this
         preserves the old get_style_rules() behaviour where e.g.
         "creative non-fiction documentary" matched "creative non-fiction".
      3. DEFAULT_STYLE
    """
    n = _norm(style)
    if not n:
        return DEFAULT_STYLE
    # 1. exact
    if n in _ALIAS_INDEX:
        return _ALIAS_INDEX[n]
    # 2. substring — prefer the longest matching alias to avoid "pov" eating
    #    "popular science" etc. Match alias-in-input AND input-in-alias.
    best_key = None
    best_len = -1
    for alias, key in _ALIAS_INDEX.items():
        if (alias in n or n in alias) and len(alias) > best_len:
            best_key, best_len = key, len(alias)
    if best_key is not None:
        return best_key
    # 3. fallback
    return DEFAULT_STYLE


def resolve_style(style: Optional[str]) -> dict:
    """Return the full registry entry for a style, with `key` injected.

    Never raises — falls back to DEFAULT_STYLE. The returned dict is a shallow
    copy so callers can safely mutate it.
    """
    key = resolve_style_key(style)
    entry = dict(STYLES[key])
    entry["key"] = key
    return entry


def resolve_language(language: Optional[str]) -> str:
    """Resolve a language code/label to its display label.

    "id" -> "Bahasa Indonesia"; an already-resolved/unknown label passes
    through trimmed; empty -> default. Mirrors _resolve_narasi_lang exactly.
    """
    if not language:
        return DEFAULT_LANGUAGE_LABEL
    return LANGUAGE_NAMES.get(language.strip().lower(), language.strip())


__all__ = [
    "LANGUAGE_NAMES",
    "DEFAULT_LANGUAGE_LABEL",
    "resolve_style_key",
    "resolve_style",
    "resolve_language",
]
