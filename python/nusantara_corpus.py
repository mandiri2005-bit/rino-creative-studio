"""
Nusantara Visual Corpus — Phase 1
Seed-based BM25-lite retrieval + visual_facts injection for image prompt enhancement.

Phase 1: JSON seed file (python/data/nusantara_seed.json), text similarity only.
Phase 2: Qdrant collection nusantara_visual_v1 with dense embeddings.
"""
import os, json, re, math, logging
from pathlib import Path
from collections import Counter
from functools import lru_cache

logger = logging.getLogger(__name__)

_SEED_PATH = Path(__file__).parent / "data" / "nusantara_seed.json"
_STOP = {"yang","dan","di","ke","dari","untuk","ini","itu","atau","dengan","ada","pada","juga",
         "the","a","an","of","in","on","at","to","for","is","are","and","or","with"}

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_TEXT_MODEL = "gemini-2.5-flash"


@lru_cache(maxsize=1)
def _load_seed() -> list[dict]:
    if not _SEED_PATH.exists():
        logger.warning("nusantara_seed.json not found at %s", _SEED_PATH)
        return []
    return json.loads(_SEED_PATH.read_text())


def _tok(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower())
            if t not in _STOP and len(t) > 1]


def _bm25(qt: list[str], doc: str) -> float:
    dt = Counter(_tok(doc))
    return sum(math.log(1 + dt[t]) for t in set(qt) if t in dt)


def retrieve(query: str, top_k: int = 2) -> list[dict]:
    """Return top-k exemplars by BM25-lite score. Returns [] if no match."""
    seed = _load_seed()
    qt = _tok(query)
    if not qt:
        return []
    scored = []
    for ex in seed:
        doc = " ".join(filter(None, [
            ex.get("subject",""), ex.get("category",""),
            ex.get("region",""), ex.get("visual_facts",""),
            " ".join(ex.get("tags", []))
        ]))
        s = _bm25(qt, doc)
        if s > 0:
            scored.append((s, ex))
    scored.sort(key=lambda x: -x[0])
    return [ex for _, ex in scored[:top_k]]


def enhance_prompt(prompt: str, gemini_api_key: str | None = None, top_k: int = 2) -> tuple[str, list[dict]]:
    """
    Retrieve exemplars and return an enhanced prompt.

    Two paths:
    - If gemini_api_key is provided: use Gemini text model to craft a rich image prompt
      (same as standalone phase1.py — best quality).
    - If not: append visual_facts inline to prompt (fallback, still useful).

    Returns (enhanced_prompt, hits).
    """
    hits = retrieve(prompt, top_k=top_k)
    if not hits:
        return prompt, []

    facts_block = "\n".join(f"- {h['subject']}: {h['visual_facts']}" for h in hits)

    if gemini_api_key:
        enhanced = _gg_enhance(prompt, facts_block, gemini_api_key)
    else:
        enhanced = f"{prompt}\n\n[Nusantara visual ref: {facts_block}]"

    logger.info("nusantara_corpus: %d hits for %r → %d→%d chars",
                len(hits), prompt[:40], len(prompt), len(enhanced))
    return enhanced, hits


def _gg_enhance(prompt: str, facts_block: str, api_key: str) -> str:
    """Call Gemini text model DIRECTLY (generativelanguage.googleapis.com) to write image prompt."""
    import requests as _req
    sys_prompt = (
        "Kamu adalah expert text-to-image prompt engineer untuk budaya visual Indonesia (Nusantara). "
        "Expand input menjadi SATU prompt gambar kaya dan terstruktur: detail subjek, komposisi, "
        "pencahayaan, palet warna, medium/gaya, quality modifiers. Sertakan detail budaya otentik. "
        "Output HANYA prompt final, satu paragraf, tanpa pembuka."
    )
    user = f"Scene: {prompt}\n\nReferensi visual Nusantara otentik:\n{facts_block}"
    body = {
        "contents": [{"parts": [{"text": user}]}],
        "systemInstruction": {"parts": [{"text": sys_prompt}]},
        "generationConfig": {
            "maxOutputTokens": 2000,
            "temperature": 0.4,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    try:
        r = _req.post(f"{_GEMINI_BASE}/{_TEXT_MODEL}:generateContent",
                      headers=headers, json=body, timeout=60)
        if r.ok:
            parts = (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            t = "".join(p.get("text", "") for p in parts).strip()
            if t:
                return t
        elif r.status_code == 400:
            # Retry without thinkingConfig (older model versions)
            body["generationConfig"].pop("thinkingConfig", None)
            r2 = _req.post(f"{_GEMINI_BASE}/{_TEXT_MODEL}:generateContent",
                           headers=headers, json=body, timeout=60)
            if r2.ok:
                parts = (r2.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
                t = "".join(p.get("text", "") for p in parts).strip()
                if t:
                    return t
        logger.warning("nusantara_corpus gg_enhance failed: %s %s", r.status_code, r.text[:80])
    except Exception as e:
        logger.warning("nusantara_corpus gg_enhance error: %s", e)

    # Fallback: inline injection
    return f"{prompt}\n\n[Nusantara visual ref: {facts_block}]"
