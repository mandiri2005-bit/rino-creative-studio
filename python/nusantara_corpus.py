"""
Nusantara Visual Corpus
Retrieval-enhanced image prompt engineering for Indonesian (Nusantara) cultural content.

Retrieval hierarchy (in order of preference):
  1. Qdrant ANN (QDRANT_URL + GEMINI_API_KEY set) — dense semantic search
  2. BM25-lite fallback from JSON seed                — always available

Usage in /generate-image handler:
  enhanced_prompt, hits, ref_b64 = _nc.enhance_prompt(prompt, gemini_api_key=GEMINI_API_KEY)
  if not req.ref_image and ref_b64:
      req.ref_image = ref_b64
"""
import os, json, re, math, base64, io, logging
from pathlib import Path
from collections import Counter
from functools import lru_cache

logger = logging.getLogger(__name__)

# ── paths ────────────────────────────────────────────────────────────────
_DATA_DIR   = Path(__file__).parent / "data"
_SEED_PATH  = _DATA_DIR / "nusantara_seed.json"
_REFS_DIR   = _DATA_DIR / "refs"

# ── external URLs ────────────────────────────────────────────────────────
_GEMINI_BASE    = "https://generativelanguage.googleapis.com/v1beta/models"
_TEXT_MODEL     = "gemini-2.5-flash"
_EMBED_MODEL    = "gemini-embedding-001"   # 3072d
_COLLECTION     = "nusantara_visual_v1"

_STOP = {"yang","dan","di","ke","dari","untuk","ini","itu","atau","dengan","ada","pada","juga",
         "the","a","an","of","in","on","at","to","for","is","are","and","or","with"}

# Scene/category words that appear in subject NAMES but are never the subject itself
# (the specific word beside them is). They must NOT qualify an entry on their own,
# else "prewedding di sawah" matches the sawah-ghost and "pura bali" matches every
# temple. A prompt still qualifies an entry via its distinctive word (bromo, kuta,
# besakih, bakso, kuntilanak, ...).
_GENERIC = {"tukang","hantu","setan","orang","pura","candi","tugu","masjid","gereja",
            "jembatan","gunung","danau","pantai","pulau","taman","rumah","kota","jalan",
            "sawah","gedung","istana","benteng","kepulauan","makhluk","kain",
            "tari","tarian","penari"}


# ── seed loader ──────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _load_seed() -> list[dict]:
    if not _SEED_PATH.exists():
        logger.warning("nusantara_seed.json not found at %s", _SEED_PATH)
        return []
    return json.loads(_SEED_PATH.read_text())

def _seed_by_id() -> dict[str, dict]:
    return {ex["id"]: ex for ex in _load_seed()}


# ── BM25-lite fallback (name-weighted, IDF, median-gated) ─────────────────
# Two signals decide a match: (1) a query word hitting an entry's NAME/subject
# (strong) vs only its description (weak), and (2) IDF, so common words ("candi",
# "gunung", "tukang", "jakarta") can't carry a match alone. An entry qualifies
# only if some query token hits its NAME with above-median specificity — so
# "borobudur saat sunrise" returns Borobudur (not Bromo, which merely has
# "sunrise" in its facts), "monas dan gedung sate" keeps BOTH, and a generic/
# off-domain prompt returns nothing. Scale-stable: the bar is the corpus's own
# median IDF, not a magic constant. Qdrant ANN (semantic) supersedes this.
_NAME_W = 2.2       # weight: query token matching the entry's name/subject
_BODY_W = 0.5       # weight: query token matching only the description/tags
_GATE_FRAC = 0.3    # among name-matched qualifiers, keep those scoring >= 30% of the
                    # best — loose on purpose: a subject explicitly named in the prompt
                    # must not be dropped just because another subject matched more words

def _tok(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower())
            if t not in _STOP and len(t) > 1]

@lru_cache(maxsize=1)
def _corpus_docs() -> tuple:
    """(exemplar, name-token frozenset, body-token tuple) per seed entry."""
    out = []
    for ex in _load_seed():
        name = ex.get("subject", "") + " " + ex.get("id", "").replace("-", " ")
        body = " ".join(filter(None, [
            ex.get("category", ""), ex.get("region", ""),
            ex.get("visual_facts", ""), " ".join(ex.get("tags", [])),
        ]))
        out.append((ex, frozenset(_tok(name)), tuple(_tok(body))))
    return tuple(out)

@lru_cache(maxsize=1)
def _idf() -> dict:
    docs = _corpus_docs()
    n = len(docs) or 1
    df = Counter()
    for _, name, body in docs:
        for t in set(name) | set(body):
            df[t] += 1
    return {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

def _score(qt: set, name: frozenset, body: tuple, idf: dict) -> float:
    bc = Counter(body)
    s = 0.0
    for t in qt:
        if t in name:
            s += _NAME_W * idf.get(t, 1.0)
        elif t in bc:
            s += _BODY_W * idf.get(t, 0.3) * (bc[t] / (bc[t] + 1.5))
    return s

def _bm25_retrieve(query: str, top_k: int) -> list[dict]:
    qt = set(_tok(query))
    if not qt:
        return []
    idf = _idf()
    cands = []
    for ex, name, body in _corpus_docs():
        # qualify only if a query token hits this entry's NAME/subject (excluding
        # generic scene words) — a match on description words alone (e.g. "sunrise"
        # in Bromo's facts) or a generic word alone ("sawah", "pura") doesn't count,
        # so off-domain & purely-descriptive prompts inject nothing
        if any(t in name and t not in _GENERIC for t in qt):
            cands.append((_score(qt, name, body, idf), ex))
    if not cands:
        return []
    cands.sort(key=lambda x: -x[0])
    top = cands[0][0]
    return [ex for s, ex in cands if s >= _GATE_FRAC * top][:top_k]


# ── Gemini embedding ─────────────────────────────────────────────────────
def _gg_embed(text: str, api_key: str, task: str = "RETRIEVAL_QUERY") -> list[float] | None:
    import requests as _req
    body = {"content": {"parts": [{"text": text}]}, "taskType": task}
    h = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    try:
        r = _req.post(f"{_GEMINI_BASE}/{_EMBED_MODEL}:embedContent",
                      headers=h, json=body, timeout=20)
        if r.ok:
            return r.json()["embedding"]["values"]
        logger.warning("embed %s: %s", r.status_code, r.text[:60])
    except Exception as e:
        logger.warning("embed err: %s", e)
    return None


# ── Qdrant ANN retrieval ─────────────────────────────────────────────────
def _qdrant_retrieve(query: str, top_k: int, api_key: str, qdrant_url: str,
                     qdrant_api_key: str) -> list[dict] | None:
    """Returns list of exemplar dicts or None on failure (triggers BM25 fallback)."""
    import requests as _req
    vec = _gg_embed(query, api_key)
    if not vec:
        return None
    body = {"vector": vec, "limit": top_k, "with_payload": True}
    h = {"Content-Type": "application/json"}
    if qdrant_api_key:
        h["api-key"] = qdrant_api_key
    try:
        r = _req.post(f"{qdrant_url.rstrip('/')}/collections/{_COLLECTION}/points/search",
                      headers=h, json=body, timeout=10)
        if not r.ok:
            logger.warning("qdrant search %s: %s", r.status_code, r.text[:80])
            return None
        results = r.json().get("result", [])
        if not results:
            return []
        seed_idx = _seed_by_id()
        top = results[0].get("score") or 0.0          # results are score-desc
        hits = []
        for pt in results:
            if top > 0 and (pt.get("score") or 0.0) < _GATE_FRAC * top:
                continue                                # same relative gate as BM25
            eid = pt.get("payload", {}).get("exemplar_id")
            if eid and eid in seed_idx:
                hits.append(seed_idx[eid])
        return hits
    except Exception as e:
        logger.warning("qdrant retrieve err: %s", e)
        return None


# ── public retrieve ──────────────────────────────────────────────────────
def retrieve(query: str, top_k: int = 8,
             gemini_api_key: str | None = None,
             qdrant_url: str | None = None,
             qdrant_api_key: str | None = None) -> list[dict]:
    """
    Return matching exemplars (capped at top_k). Tries Qdrant ANN first if
    configured, falls back to BM25. A relative score gate trims weak matches, so
    the count adapts to the prompt (1 subject → ~1 hit, 5 subjects → ~5).
    """
    if gemini_api_key and qdrant_url:
        hits = _qdrant_retrieve(query, top_k, gemini_api_key, qdrant_url, qdrant_api_key or "")
        if hits is not None:
            logger.info("nusantara_corpus: qdrant %d hits for %r", len(hits), query[:40])
            return hits
        logger.info("nusantara_corpus: qdrant failed, falling back to BM25")

    hits = _bm25_retrieve(query, top_k)
    logger.info("nusantara_corpus: bm25 %d hits for %r", len(hits), query[:40])
    return hits


# ── ref image (image-conditioning) ───────────────────────────────────────
def load_ref_b64(exemplar_id: str, max_px: int = 512) -> str | None:
    """Load and return base64-encoded JPEG thumbnail for image-conditioning."""
    ref_path = _REFS_DIR / f"{exemplar_id}.jpg"
    if not ref_path.exists():
        return None
    try:
        from PIL import Image
        im = Image.open(ref_path).convert("RGB")
        im.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        logger.warning("load_ref_b64 %s: %s", exemplar_id, e)
    return None


# ── prompt enhancement ────────────────────────────────────────────────────
def enhance_prompt(
    prompt: str,
    gemini_api_key: str | None = None,
    qdrant_url: str | None = None,
    qdrant_api_key: str | None = None,
    top_k: int = 8,
) -> tuple[str, list[dict], str | None]:
    """
    Retrieve exemplars → build enhanced image prompt → load ref image.

    Returns (enhanced_prompt, hits, ref_b64_or_None).
    - enhanced_prompt: richer prompt with Nusantara visual facts injected.
    - hits: matched exemplar dicts (empty list if no match).
    - ref_b64: base64 JPEG for image-conditioning (None if no ref thumbnail).

    Enhancement path:
      - With gemini_api_key: Gemini text model crafts a full rich prompt (best quality).
      - Without: visual_facts appended inline (useful fallback).
    """
    hits = retrieve(prompt, top_k=top_k,
                    gemini_api_key=gemini_api_key,
                    qdrant_url=qdrant_url,
                    qdrant_api_key=qdrant_api_key)
    if not hits:
        return prompt, [], None

    facts_block = "\n".join(f"- {h['subject']}: {h['visual_facts']}" for h in hits)

    if gemini_api_key:
        enhanced = _gg_enhance(prompt, facts_block, gemini_api_key)
    else:
        enhanced = f"{prompt}\n\n[Nusantara visual ref: {facts_block}]"

    # Load ref image from top hit for image-conditioning
    ref_b64 = load_ref_b64(hits[0]["id"])

    logger.info("nusantara_corpus: %d hits, ref=%s, %d→%d chars",
                len(hits), hits[0]["id"] if ref_b64 else "none",
                len(prompt), len(enhanced))
    return enhanced, hits, ref_b64


# ── Gemini text enhance ───────────────────────────────────────────────────
def _gg_enhance(prompt: str, facts_block: str, api_key: str) -> str:
    """Call Gemini text model DIRECTLY to write a rich image prompt."""
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
            "maxOutputTokens": 2000, "temperature": 0.4,
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
            body["generationConfig"].pop("thinkingConfig", None)
            r2 = _req.post(f"{_GEMINI_BASE}/{_TEXT_MODEL}:generateContent",
                           headers=headers, json=body, timeout=60)
            if r2.ok:
                parts = (r2.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
                t = "".join(p.get("text", "") for p in parts).strip()
                if t:
                    return t
        logger.warning("gg_enhance failed: %s %s", r.status_code, r.text[:80])
    except Exception as e:
        logger.warning("gg_enhance error: %s", e)
    return f"{prompt}\n\n[Nusantara visual ref: {facts_block}]"
