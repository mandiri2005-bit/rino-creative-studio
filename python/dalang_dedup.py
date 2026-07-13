"""
Dalang v2 Slice 5 — chapter repetition guard + episode embedding index.

Two mechanisms, both fail-OPEN (never crash a generation job):
  1. find_repetition() — deterministic 5-gram Jaccard over the series' prior chapters
     (in-memory, ALWAYS available). This is the reliable gate (§4.3).
  2. Qdrant `dalang_episodes` — event-driven per-chapter embedding upsert + semantic
     search, mirroring nusantara_corpus.py (raw-HTTP, stable int ids, score gate, 3072-dim
     Cosine gemini-embedding-001). Powers cross-episode dedup for Showrunner; a best-effort
     ENHANCEMENT for Dalang. Qdrant/embed unavailable ⟹ silently skipped (the n-gram gate
     still runs), never raising into the job.

Nothing here runs unless DALANG_EMBED_DEDUP_ENABLED (checked by the caller).
"""
import os
import logging
import hashlib

log = logging.getLogger("dalang_dedup")

_COLLECTION     = "dalang_episodes"
_DIM            = 3072                                  # gemini-embedding-001 (matches nusantara)
_QDRANT_URL     = os.getenv("QDRANT_URL", "").strip()
_QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "").strip()
_GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()


# ── deterministic n-gram repetition (the reliable gate) ──────────────────────────────
def _ngrams(text: str, n: int = 5) -> set:
    toks = (text or "").lower().split()
    if len(toks) < n:
        return {" ".join(toks)} if toks else set()
    return {" ".join(toks[i:i + n]) for i in range(len(toks) - n + 1)}


def ngram_jaccard(a: str, b: str, n: int = 5) -> float:
    A, B = _ngrams(a, n), _ngrams(b, n)
    if not A or not B:
        return 0.0
    return len(A & B) / float(len(A | B))


def find_repetition(text: str, prior_texts: list, threshold: float = 0.18) -> dict:
    """Deterministic gate: 5-gram Jaccard of `text` vs each prior chapter. Returns
    {duplicate, score, of_index}. Pure — no I/O, always available."""
    best_s, best_i = 0.0, -1
    for idx, prev in enumerate(prior_texts or []):
        s = ngram_jaccard(text, prev)
        if s > best_s:
            best_s, best_i = s, idx
    return {"duplicate": best_s >= threshold and best_i >= 0,
            "score": round(best_s, 4), "of_index": best_i}


# ── Qdrant episode index (best-effort, fail-open) ────────────────────────────────────
def _point_id(series_id, episode_index: int) -> int:
    h = hashlib.md5(f"{series_id}:{episode_index}".encode()).hexdigest()
    return int(h[:15], 16)          # stable positive int id (mirrors nusantara _point_id)


def qdrant_ready() -> bool:
    return bool(_QDRANT_URL)


def _embed(text: str, task: str = "SEMANTIC_SIMILARITY"):
    """gemini-embedding-001 embed. Chapter dedup is SYMMETRIC (chapter-vs-chapter), so BOTH
    the query and the index side use SEMANTIC_SIMILARITY → the two vectors stay in one
    comparable space (mixing RETRIEVAL_QUERY/DOCUMENT would put them in asymmetric subspaces
    and weaken the cosine). None on any failure (fail-open)."""
    if not (text or "").strip():
        return None
    # ROUND-8 / corpus fix (user-authorized): the 403 flood — this embedder still used the
    # raw GEMINI key while the project's credentials moved to Vertex OAuth; gg_enhance got
    # the OAuth route in 3821242 but the dedup embed path never did. Prefer the proven
    # OAuth embedder (lazy import — laozhang imports this module at load, so a top-level
    # import back would be circular); fall back to the key path only if OAuth is absent.
    try:
        from laozhang_api import _vertex_embed as _vx   # lazy: avoids circular import
        _v = _vx(text[:8000], task=task)
        if _v:
            return _v
    except Exception as e:  # noqa: BLE001
        log.warning("dedup embed (oauth) err — falling back to key path: %s", e)
    if not _GEMINI_API_KEY:
        return None
    try:
        from nusantara_corpus import _gg_embed         # legacy key-based embedder
        return _gg_embed(text[:8000], _GEMINI_API_KEY, task=task)
    except Exception as e:
        log.warning("dedup embed err: %s", e)
        return None


def embed(text: str):
    """Public: embed one chapter (SEMANTIC_SIMILARITY). Compute ONCE and reuse for both the
    search and the upsert to avoid a double embed of the same text."""
    return _embed(text)


def _headers():
    h = {"Content-Type": "application/json"}
    if _QDRANT_API_KEY:
        h["api-key"] = _QDRANT_API_KEY
    return h


def _ensure_collection():
    import requests as _req
    try:
        _req.put(f"{_QDRANT_URL.rstrip('/')}/collections/{_COLLECTION}", headers=_headers(),
                 json={"vectors": {"size": _DIM, "distance": "Cosine"}}, timeout=15)  # no-op if exists
    except Exception as e:
        log.warning("dedup ensure-collection err: %s", e)


def index_chapter(text: str, series_id, tenant_id, episode_index: int, vec=None) -> bool:
    """Event-driven upsert of one chapter's embedding into `dalang_episodes`. Reuses a
    precomputed `vec` when given (halves embed spend on the no-rewrite path); else embeds
    `text`. Fail-open → False on any issue (no Qdrant, no key, embed/HTTP error)."""
    if not qdrant_ready():
        return False
    vec = vec or _embed(text)
    if not vec:
        return False
    import requests as _req
    try:
        _ensure_collection()
        pt = {"id": _point_id(series_id, episode_index), "vector": vec,
              "payload": {"series_id": str(series_id), "tenant_id": str(tenant_id),
                          "episode_index": int(episode_index)}}
        r = _req.put(f"{_QDRANT_URL.rstrip('/')}/collections/{_COLLECTION}/points",
                     headers=_headers(), json={"points": [pt]}, timeout=15)
        return bool(getattr(r, "ok", False))
    except Exception as e:
        log.warning("dedup upsert err: %s", e)
        return False


def search_vec(vec, series_id, tenant_id, episode_index: int, threshold: float = 0.86):
    """Qdrant semantic search given a PRECOMPUTED vector (excludes self within the series).
    Returns {duplicate, score, of_index} on success, or None on any failure so the caller
    falls back to the deterministic n-gram gate."""
    if not qdrant_ready() or not vec:
        return None
    import requests as _req
    # F9 (defense-in-depth): scope the shared dalang_episodes collection to BOTH series_id AND
    # tenant_id. series_id is a server-generated per-tenant UUID today (so no leak yet), but an
    # explicit tenant_id predicate hard-guards any future caller (Showrunner/Persona) that reuses
    # a shared/client-influenced series_id. tenant_id is already written to the payload at upsert.
    flt = {"must":     [{"key": "series_id", "match": {"value": str(series_id)}},
                        {"key": "tenant_id", "match": {"value": str(tenant_id)}}],
           "must_not": [{"key": "episode_index", "match": {"value": int(episode_index)}}]}
    try:
        r = _req.post(f"{_QDRANT_URL.rstrip('/')}/collections/{_COLLECTION}/points/search",
                      headers=_headers(),
                      json={"vector": vec, "limit": 3, "with_payload": True, "filter": flt},
                      timeout=15)
        if not getattr(r, "ok", False):
            return None
        res = r.json().get("result", [])
        if not res:
            return None
        top = res[0]
        score = float(top.get("score") or 0.0)
        return {"duplicate": score >= threshold, "score": round(score, 4),
                "of_index": (top.get("payload", {}) or {}).get("episode_index", -1)}
    except Exception as e:
        log.warning("dedup search err: %s", e)
        return None


def semantic_repetition(text: str, series_id, tenant_id, episode_index: int, threshold: float = 0.86):
    """Convenience: embed `text` then search. The loop calls embed()+search_vec() directly so
    it can reuse the vector for the upsert; kept for standalone/testing use."""
    return search_vec(_embed(text), series_id, tenant_id, episode_index, threshold)
