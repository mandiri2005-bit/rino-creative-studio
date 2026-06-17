"""ceritaAI FAQ knowledge base + lightweight retrieval (BM25-lite, no embeddings).

The KB is small (~30 entries) so a keyword-overlap scorer is plenty and needs zero
infra. Used by the /faq/ask endpoint: retrieve top-k relevant entries, then a
grounded LLM (Gemini 2.5 Flash via Vertex OAuth) answers ONLY from them.
"""
import json, os, re, functools

_FAQ_PATH = os.path.join(os.path.dirname(__file__), "data", "faq_kb.json")

# Indonesian + generic stopwords so scoring keys on meaningful tokens.
_STOP = {
    "yang","di","ke","dari","dan","atau","untuk","dengan","ini","itu","apa","cara",
    "gimana","gmn","bagaimana","kenapa","kenapa","kalau","kalo","bisa","gak","ga","nggak",
    "tidak","ada","aku","saya","mau","pakai","pake","make","nya","sih","dong","ya","aja",
    "the","a","an","to","of","is","how","what","do","i","my","can","in","on","for",
}

def _tok(s):
    return [t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in _STOP and len(t) > 1]

@functools.lru_cache(maxsize=1)
def _load():
    try:
        with open(_FAQ_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    out = []
    for e in data:
        out.append({
            **e,
            "_q": _tok(e.get("q", "")),
            "_kw": _tok(e.get("keywords", "")),
            "_a": _tok(e.get("a", "")),
        })
    return out

def reload_kb():
    _load.cache_clear()
    return len(_load())

def count():
    return len(_load())

def retrieve(question, k=8):
    """Return up to k most relevant FAQ entries for `question`.

    Scoring: token overlap weighted q(×3) + keywords(×2) + answer(×1). Entries
    with zero overlap are dropped (so the LLM gets a clean, relevant context).
    """
    qt = set(_tok(question))
    if not qt:
        return []
    scored = []
    for e in _load():
        s = 3 * len(qt & set(e["_q"])) + 2 * len(qt & set(e["_kw"])) + 1 * len(qt & set(e["_a"]))
        if s > 0:
            scored.append((s, e))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:k]]

def build_context(entries):
    """Render retrieved entries into a compact grounding block for the LLM."""
    blocks = []
    for e in entries:
        blocks.append(f"[{e.get('topic','')}] T: {e.get('q','')}\nJ: {e.get('a','')}")
    return "\n\n".join(blocks)
