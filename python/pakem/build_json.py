# -*- coding: utf-8 -*-
"""pakem.build_json — compile the pakem catalog to a static JSON (Project Dalang WS-3).

Emits `python/pakem/pakem.json`: the small, serializable view of the pakem canon
that Node (backend/server.js) and the frontend picker need — styles (value+label+
is_fiction), languages (value+label), and PAKEM_VERSION. The heavy rule blocks
(style_rules_core / _editor, RAG framing) stay OUT of this file; they are only ever
assembled server-side via /narration/prompt. This keeps the wire payload tiny and
prevents the long prompts from leaking to the client.

The same builder functions (`styles_catalog`, `languages_catalog`, `build_catalog`)
back the GET /narration/styles, GET /narration/languages, and the pakem.json file —
so the static file and the live endpoints can never drift.

Run:  python3 pakem/build_json.py        # writes pakem/pakem.json
      python3 -m pakem.build_json         # same, as a module
"""
from __future__ import annotations

import json
import os
from typing import Any

if __package__:  # run as a module/package: python3 -m pakem.build_json
    from . import PAKEM_VERSION, STYLES
    from .resolvers import LANGUAGE_NAMES
else:  # run as a script: python3 pakem/build_json.py (pakem/ dir is on sys.path, python/ is not)
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from pakem import PAKEM_VERSION, STYLES  # type: ignore
    from pakem.resolvers import LANGUAGE_NAMES  # type: ignore


# Default output path: alongside this module (python/pakem/pakem.json).
OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pakem.json")


def styles_catalog() -> list[dict[str, Any]]:
    """The picker-facing style list: value + label + is_fiction, in registry order.

    `value` is the canonical registry key (stable, resolver-friendly — feeding it
    back into resolve_style() is an identity match). `label` is the human display
    name. `is_fiction` lets the UI/Node skip factual-integrity affordances.
    """
    out: list[dict[str, Any]] = []
    for key, entry in STYLES.items():
        out.append({
            "value": key,
            "label": entry.get("display_name", key),
            "is_fiction": bool(entry.get("is_fiction", False)),
        })
    return out


def languages_catalog() -> list[dict[str, str]]:
    """The picker-facing language list: value (code) + label (display name)."""
    return [{"value": code, "label": label} for code, label in LANGUAGE_NAMES.items()]


def build_catalog() -> dict[str, Any]:
    """The full serializable pakem view written to pakem.json and served by the endpoints."""
    return {
        "PAKEM_VERSION": PAKEM_VERSION,
        "styles": styles_catalog(),
        "languages": languages_catalog(),
    }


def write(path: str = OUTPUT_PATH) -> str:
    """Write the catalog JSON to `path` (pretty-printed, UTF-8, trailing newline)."""
    catalog = build_catalog()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(catalog, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


def main() -> None:
    path = write()
    catalog = build_catalog()
    print(
        f"wrote {path}: PAKEM_VERSION={catalog['PAKEM_VERSION']} "
        f"styles={len(catalog['styles'])} languages={len(catalog['languages'])}"
    )


if __name__ == "__main__":
    main()
