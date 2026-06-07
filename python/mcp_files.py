"""
LaoZhang MCP — Folder Reader v3  (Hybrid Semantic + BM25 RAG)

Search modes (set via --search flag or SEARCH_MODE env var):
  bm25      — original keyword matching (fast, no deps)
  local     — BAAI/bge-m3 local embeddings (best multilingual, ~2GB download)
  api       — OpenAI-compatible embeddings via laozhang.ai (text-embedding-3-small)
  hybrid    — BM25 + semantic via RRF fusion (recommended for mixed queries)

Run:
  python mcp_files.py --folder ~/Downloads --search hybrid
  python mcp_files.py --folder ~/Downloads --search api --embed-model text-embedding-3-large

Install deps:
  pip install fastapi uvicorn                         # always required
  pip install sentence-transformers numpy             # for local / hybrid
  pip install openai numpy                            # for api / hybrid
  pip install pymupdf                                 # for PDF support
"""

import os
import sys
import re
import json
import math
import pickle
import hashlib
import argparse
import threading
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ---------------------------------------------------------------------------
# Config defaults  (all overridable via CLI args)
# ---------------------------------------------------------------------------
SUPPORTED = {
    ".txt", ".md", ".markdown", ".csv", ".json",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".xml",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh", ".bat",
    ".log", ".rst", ".tex", ".sql", ".go", ".rs", ".rb", ".java",
    ".cpp", ".c", ".h", ".swift", ".kt", ".r", ".php",
    ".srt", ".vtt", ".sub", ".diff", ".patch", ".pdf",
}
MAX_FILE_SIZE_MB  = 10
CHUNK_SIZE        = 600
CHUNK_OVERLAP     = 100
TOP_K_CHUNKS      = 25
MAX_CONTEXT_CHARS = 60_000

# Embedding API defaults (laozhang.ai-compatible)
EMBED_API_BASE    = "https://api.laozhang.ai/v1"
EMBED_API_KEY     = os.environ.get("LAOZHANG_API_KEY", "")
EMBED_MODEL_API   = "text-embedding-3-small"   # change to 3-large for higher quality
EMBED_MODEL_LOCAL = "BAAI/bge-m3"              # best multilingual local model

# Cache file (stores chunk embeddings so restarts are instant)
CACHE_FILE = ".mcp_embed_cache.pkl"

# Runtime globals (set in main)
FOLDER: Path       = None
SEARCH_MODE: str   = "bm25"   # bm25 | local | api | hybrid
_index: list[dict] = []
_index_built       = False
_local_model       = None      # SentenceTransformer instance (lazy-loaded)

# ---------------------------------------------------------------------------
app = FastAPI(title="LaoZhang MCP — Folder Reader v3")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------
def get_all_files() -> list[dict]:
    result = []
    for root, dirs, files in os.walk(FOLDER):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in sorted(files):
            if fname.startswith("."):
                continue
            fpath = Path(root) / fname
            if fpath.suffix.lower() not in SUPPORTED:
                continue
            size = fpath.stat().st_size
            result.append({
                "name":     fname,
                "path":     str(fpath.relative_to(FOLDER)),
                "abs_path": str(fpath),
                "ext":      fpath.suffix.lower(),
                "size_kb":  round(size / 1024, 1),
            })
    return result


def safe_read(rel_path: str) -> str:
    fpath = (FOLDER / rel_path).resolve()
    if not str(fpath).startswith(str(FOLDER.resolve())):
        raise HTTPException(403, "Access denied")
    if not fpath.exists():
        raise HTTPException(404, "File not found")
    size_mb = fpath.stat().st_size / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(413, f"File too large (max {MAX_FILE_SIZE_MB} MB)")
    if fpath.suffix.lower() == ".pdf":
        try:
            import fitz
            doc = fitz.open(str(fpath))
            return "\n".join(page.get_text() for page in doc)
        except ImportError:
            raise HTTPException(500, "pymupdf not installed — run: pip install pymupdf")
    return fpath.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
def chunk_text(text: str, path: str) -> list[dict]:
    chunks, start, idx = [], 0, 0
    while start < len(text):
        chunk = text[start: start + CHUNK_SIZE]
        chunks.append({"path": path, "chunk_idx": idx, "text": chunk})
        idx += 1
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


# ---------------------------------------------------------------------------
# BM25 helpers  (always available, used in hybrid mode too)
# ---------------------------------------------------------------------------
def tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+", text.lower()))


def bm25_score(query_terms: set[str], text: str) -> float:
    words = re.findall(r"[a-z0-9_]+", text.lower())
    if not words:
        return 0.0
    total = len(words)
    freq: dict[str, int] = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    score = 0.0
    for term in query_terms:
        tf = freq.get(term, 0)
        if tf:
            score += (tf / (tf + 1.5)) * math.log(1 + total / (tf + 1))
    return score


def bm25_search(query: str, top_k: int = TOP_K_CHUNKS) -> list[dict]:
    q_terms = tokenize(query)
    if not q_terms:
        return _index[:top_k]
    scored = sorted(
        ((bm25_score(q_terms, c["text"]), c) for c in _index),
        key=lambda x: x[0], reverse=True,
    )
    results, seen = [], set()
    for score, chunk in scored:
        if score == 0:
            break
        key = (chunk["path"], chunk["chunk_idx"])
        if key not in seen:
            seen.add(key)
            results.append(chunk)
            if len(results) >= top_k:
                break
    return results


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------
def _load_local_model():
    global _local_model
    if _local_model is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise RuntimeError(
                "sentence-transformers not installed.\n"
                "Run: pip install sentence-transformers"
            )
        print(f"  Loading local model: {EMBED_MODEL_LOCAL}  (first run downloads ~2GB)")
        _local_model = SentenceTransformer(EMBED_MODEL_LOCAL)
        print("  Local model ready.")
    return _local_model


def embed_local(texts: list[str]) -> np.ndarray:
    model = _load_local_model()
    return model.encode(texts, batch_size=32, show_progress_bar=len(texts) > 100,
                        normalize_embeddings=True)


def embed_api(texts: list[str]) -> np.ndarray:
    if not EMBED_API_KEY:
        raise RuntimeError(
            "LAOZHANG_API_KEY env var not set — required for API embedding mode."
        )
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("openai package not installed — run: pip install openai")

    client = OpenAI(api_key=EMBED_API_KEY, base_url=EMBED_API_BASE)

    # laozhang.ai supports batch, but cap at 512 texts per call to be safe
    all_embeddings = []
    batch_size = 512
    for i in range(0, len(texts), batch_size):
        batch = texts[i: i + batch_size]
        resp = client.embeddings.create(model=EMBED_MODEL_API, input=batch)
        batch_embs = [np.array(item.embedding) for item in sorted(resp.data, key=lambda x: x.index)]
        all_embeddings.extend(batch_embs)

    matrix = np.array(all_embeddings, dtype=np.float32)
    # Normalize for cosine similarity
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.maximum(norms, 1e-9)


def embed_texts(texts: list[str]) -> np.ndarray:
    """Route to local or API embedder based on SEARCH_MODE."""
    if SEARCH_MODE in ("api", "hybrid") and EMBED_API_KEY:
        return embed_api(texts)
    return embed_local(texts)


def cosine_similarity_bulk(query_vec: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Fast vectorized cosine similarity (query already normalized)."""
    return matrix @ query_vec


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------
def _folder_hash() -> str:
    files = get_all_files()
    fingerprint = str([(f["path"], f["size_kb"]) for f in files])
    return hashlib.md5(fingerprint.encode()).hexdigest()


def _load_cache() -> Optional[list[dict]]:
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, "rb") as f:
            cached = pickle.load(f)
        if (cached.get("folder_hash") == _folder_hash()
                and cached.get("search_mode") == SEARCH_MODE
                and cached.get("embed_model") == (EMBED_MODEL_API if SEARCH_MODE == "api" else EMBED_MODEL_LOCAL)):
            return cached["index"]
    except Exception:
        pass
    return None


def _save_cache(index: list[dict]):
    try:
        with open(CACHE_FILE, "wb") as f:
            pickle.dump({
                "folder_hash": _folder_hash(),
                "search_mode": SEARCH_MODE,
                "embed_model": EMBED_MODEL_API if SEARCH_MODE == "api" else EMBED_MODEL_LOCAL,
                "index": index,
            }, f)
        print(f"  Cache saved → {CACHE_FILE}")
    except Exception as e:
        print(f"  Warning: could not save cache: {e}")


# ---------------------------------------------------------------------------
# Index builder
# ---------------------------------------------------------------------------
def build_index():
    global _index, _index_built
    _index = []

    # Try cache first
    if SEARCH_MODE != "bm25":
        cached = _load_cache()
        if cached is not None:
            _index = cached
            _index_built = True
            print(f"  Cache hit — loaded {len(_index)} chunks (no re-embedding needed)")
            return

    # Build fresh
    all_chunks = []
    for f in get_all_files():
        try:
            content = safe_read(f["path"])
            all_chunks.extend(chunk_text(content, f["path"]))
        except Exception:
            pass

    _index = all_chunks
    print(f"  Chunked {len(get_all_files())} files → {len(_index)} chunks")

    if SEARCH_MODE != "bm25":
        texts = [c["text"] for c in _index]
        print(f"  Embedding {len(texts)} chunks via {'API' if SEARCH_MODE == 'api' else 'local model'}…")
        embeddings = embed_texts(texts)
        for i, emb in enumerate(embeddings):
            _index[i]["embedding"] = emb
        _save_cache(_index)

    _index_built = True
    print(f"  Index ready: {len(_index)} chunks | mode: {SEARCH_MODE}")


# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------
def semantic_search(query: str, top_k: int = TOP_K_CHUNKS) -> list[dict]:
    """Vector similarity search over embedded chunks."""
    q_vec = embed_texts([query])[0]
    matrix = np.stack([c["embedding"] for c in _index])
    scores = cosine_similarity_bulk(q_vec, matrix)
    top_indices = np.argsort(scores)[::-1][:top_k]
    return [_index[i] for i in top_indices]


# ---------------------------------------------------------------------------
# Hybrid search — Reciprocal Rank Fusion (RRF)
# ---------------------------------------------------------------------------
def hybrid_search(query: str, top_k: int = TOP_K_CHUNKS) -> list[dict]:
    """
    Combine BM25 (exact keyword) + semantic (meaning) scores via RRF.
    BM25 excels at proper nouns / exact terms; semantic excels at paraphrasing.
    RRF weight k=60 is the standard default from the original paper.
    """
    K = 60
    sem_results  = semantic_search(query, top_k * 2)
    bm25_results = bm25_search(query, top_k * 2)

    rrf_scores: dict[tuple, float] = {}

    def key(chunk):
        return (chunk["path"], chunk["chunk_idx"])

    for rank, chunk in enumerate(sem_results):
        k = key(chunk)
        rrf_scores[k] = rrf_scores.get(k, 0.0) + 1.0 / (K + rank)

    for rank, chunk in enumerate(bm25_results):
        k = key(chunk)
        rrf_scores[k] = rrf_scores.get(k, 0.0) + 1.0 / (K + rank)

    sorted_keys = sorted(rrf_scores, key=rrf_scores.get, reverse=True)[:top_k]
    index_map = {key(c): c for c in _index}
    return [index_map[k] for k in sorted_keys if k in index_map]


# ---------------------------------------------------------------------------
# Unified search dispatcher
# ---------------------------------------------------------------------------
def search_chunks(query: str, top_k: int = TOP_K_CHUNKS) -> list[dict]:
    if not _index_built:
        build_index()
    if not query.strip():
        return _index[:top_k]

    if SEARCH_MODE == "bm25":
        return bm25_search(query, top_k)
    elif SEARCH_MODE == "hybrid":
        return hybrid_search(query, top_k)
    else:  # local or api
        return semantic_search(query, top_k)


# ---------------------------------------------------------------------------
# Context builder  (same interface as v2)
# ---------------------------------------------------------------------------
def build_smart_context(query: str, paths: Optional[list[str]] = None) -> tuple[str, int]:
    global _index_built
    if not _index_built:
        build_index()

    # Always make a copy — never alias _index directly (causes swap-clear bug)
    working_index = [c for c in _index if c["path"] in paths] if paths else list(_index)
    if not working_index:
        return "", 0

    if query.strip():
        # Search directly on working_index without swapping global _index
        q = query.strip()
        q_terms = tokenize(q)

        if SEARCH_MODE == "bm25":
            scored = sorted(
                ((bm25_score(q_terms, c["text"]), c) for c in working_index),
                key=lambda x: x[0], reverse=True,
            )
            top_chunks = [c for score, c in scored if score > 0][:TOP_K_CHUNKS]
            if not top_chunks:
                top_chunks = working_index[:TOP_K_CHUNKS]

        elif SEARCH_MODE in ("hybrid", "local", "api"):
            # Semantic search on working subset
            try:
                q_vec  = embed_texts([q])[0]
                matrix = np.stack([c["embedding"] for c in working_index])
                scores = cosine_similarity_bulk(q_vec, matrix)
                top_idx = np.argsort(scores)[::-1][:TOP_K_CHUNKS * 2]
                sem_results = [working_index[i] for i in top_idx]
            except Exception:
                sem_results = working_index[:TOP_K_CHUNKS * 2]

            if SEARCH_MODE == "hybrid":
                # RRF fusion with BM25 on working subset
                bm25_scored = sorted(
                    ((bm25_score(q_terms, c["text"]), c) for c in working_index),
                    key=lambda x: x[0], reverse=True,
                )
                bm25_results = [c for _, c in bm25_scored if _ > 0][:TOP_K_CHUNKS * 2]
                K = 60
                rrf: dict[tuple, float] = {}
                for rank, c in enumerate(sem_results):
                    k = (c["path"], c["chunk_idx"]); rrf[k] = rrf.get(k, 0) + 1/(K+rank)
                for rank, c in enumerate(bm25_results):
                    k = (c["path"], c["chunk_idx"]); rrf[k] = rrf.get(k, 0) + 1/(K+rank)
                idx_map = {(c["path"], c["chunk_idx"]): c for c in working_index}
                sorted_keys = sorted(rrf, key=rrf.get, reverse=True)[:TOP_K_CHUNKS]
                top_chunks = [idx_map[k] for k in sorted_keys if k in idx_map]
            else:
                top_chunks = sem_results[:TOP_K_CHUNKS]
        else:
            top_chunks = working_index[:TOP_K_CHUNKS]

        # Group by file, preserve within-file order
        by_file: dict[str, list] = {}
        for c in top_chunks:
            by_file.setdefault(c["path"], []).append(c)
        for p in by_file:
            by_file[p].sort(key=lambda c: c["chunk_idx"])

        parts, total = [], 0
        for p, chunks in by_file.items():
            header = f"=== {p} (relevant excerpts — {SEARCH_MODE} search) ===\n"
            body   = "\n…\n".join(c["text"] for c in chunks)
            block  = header + body + "\n"
            if total + len(block) > MAX_CONTEXT_CHARS:
                remaining = MAX_CONTEXT_CHARS - total
                if remaining > 200:
                    parts.append(block[:remaining] + "\n[truncated]")
                break
            parts.append(block)
            total += len(block)
        ctx = "\n".join(parts)
    else:
        all_files = get_all_files()
        if paths:
            all_files = [f for f in all_files if f["path"] in paths]
        parts = [f"Folder contains {len(all_files)} files:\n"]
        total = len(parts[0])
        for f in all_files:
            try:
                preview = safe_read(f["path"])[:300]
            except Exception:
                preview = "[unreadable]"
            block = f"--- {f['path']} ({f['size_kb']}KB) ---\n{preview}\n…\n"
            if total + len(block) > MAX_CONTEXT_CHARS:
                parts.append(f"[…{len(all_files) - len(parts)} more files not shown]")
                break
            parts.append(block)
            total += len(block)
        ctx = "\n".join(parts)

    return ctx, len(ctx)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
async def root():
    return {
        "status":       "ok",
        "folder":       str(FOLDER),
        "files":        len(get_all_files()),
        "index_chunks": len(_index),
        "index_built":  _index_built,
        "search_mode":  SEARCH_MODE,
        "embed_model":  EMBED_MODEL_API if SEARCH_MODE == "api" else EMBED_MODEL_LOCAL,
    }


@app.get("/files")
async def list_files():
    return {"folder": str(FOLDER), "files": get_all_files()}


@app.get("/file")
async def read_one(path: str):
    content = safe_read(path)
    return {"path": path, "content": content, "chars": len(content)}


@app.get("/context")
async def get_context(paths: str = "", q: str = ""):
    """
    Smart context endpoint.
    ?paths=a.py,b.md  — restrict to these files (optional)
    ?q=your question  — semantic/hybrid/bm25 search to pick relevant chunks
    """
    selected = [p.strip() for p in paths.split(",") if p.strip()] if paths else None
    ctx, length = build_smart_context(query=q, paths=selected)
    return {
        "context":     ctx,
        "length":      length,
        "chunks":      len(_index),
        "search_mode": SEARCH_MODE,
    }


@app.get("/search")
async def search_files(q: str):
    """Keyword search — returns matching lines per file (always BM25)."""
    q_lower = q.lower()
    results = []
    for f in get_all_files():
        try:
            content = safe_read(f["path"])
            hits = [
                {"line": i + 1, "text": line.strip()}
                for i, line in enumerate(content.splitlines())
                if q_lower in line.lower()
            ]
            if hits:
                results.append({"file": f["path"], "hits": hits[:10]})
        except Exception:
            pass
    return {"query": q, "results": results}


@app.get("/search_semantic")
async def search_semantic(q: str, top_k: int = TOP_K_CHUNKS):
    """Semantic/hybrid chunk search — returns scored chunk excerpts."""
    if not _index_built:
        build_index()
    results = search_chunks(q, top_k)
    return {
        "query":       q,
        "mode":        SEARCH_MODE,
        "results": [
            {"path": c["path"], "chunk_idx": c["chunk_idx"], "text": c["text"][:300]}
            for c in results
        ],
    }


@app.post("/reindex")
async def reindex():
    """Force rebuild the chunk index (call after adding files, or to switch mode)."""
    global _index_built
    _index_built = False
    _index.clear()
    # Remove cache so embeddings are regenerated
    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)
    build_index()
    return {"status": "ok", "chunks": len(_index), "search_mode": SEARCH_MODE}


@app.post("/set-folder")
async def set_folder_endpoint(body: dict):
    """Hot-swap the watched folder and trigger background reindex — no restart needed."""
    global FOLDER, _index, _index_built
    folder_raw = (body.get("folder") or "").strip()
    if not folder_raw:
        raise HTTPException(400, "folder is required")

    # Allow relative paths: resolve against the base /mcp-folder mount
    p = Path(folder_raw).expanduser()
    if not p.is_absolute():
        base = Path("/mcp-folder")
        p = (base / p).resolve()
    else:
        p = p.resolve()

    if not p.exists():
        raise HTTPException(404, f"Folder not found: {p}")
    if not p.is_dir():
        raise HTTPException(400, f"Not a directory: {p}")

    FOLDER = p
    _index_built = False
    _index.clear()
    # Clear embedding cache so next index is fresh
    if os.path.exists(CACHE_FILE):
        try:
            os.remove(CACHE_FILE)
        except OSError:
            pass

    # Reindex in background so the request returns immediately
    threading.Thread(target=build_index, daemon=True).start()

    return {"ok": True, "folder": str(FOLDER), "status": "reindexing"}


# ---------------------------------------------------------------------------
# SRT helpers
# ---------------------------------------------------------------------------

def _parse_srt(text: str) -> list[dict]:
    """Split SRT into blocks keeping num + timestamp intact."""
    blocks = []
    # Normalize line endings, then split on blank lines
    for raw in re.split(r"\n{2,}", text.strip().replace("\r\n", "\n")):
        lines = raw.strip().splitlines()
        if len(lines) < 3:
            continue
        num       = lines[0].strip()
        timestamp = lines[1].strip()
        subtitle  = "\n".join(lines[2:])
        blocks.append({"num": num, "timestamp": timestamp, "text": subtitle})
    return blocks


def _rebuild_srt(blocks: list[dict]) -> str:
    """Reconstruct SRT string from corrected blocks."""
    return "\n\n".join(
        f"{b['num']}\n{b['timestamp']}\n{b['text']}"
        for b in blocks
    ) + "\n"


def _fix_cascade_merges(corrected: list[str], chunk_texts: list[str]) -> tuple[list[str], int]:
    """
    Detect and fix blocks where AI merged next block's text into current block,
    causing a cascade shift. Iterates until no more merges are found.
    Returns (fixed_list, num_fixes_applied).
    """
    def wtoks(text: str) -> list[str]:
        return re.findall(r"[\w]+", text.lower())

    def sim(a: str, b: str) -> float:
        ta, tb = set(wtoks(a)), set(wtoks(b))
        return len(ta & tb) / len(tb) if tb else 0.0

    fixed = list(corrected)
    n     = len(chunk_texts)
    total = 0

    for _pass in range(15):          # max 15 sweeps
        fixes = 0
        i = 0
        while i < n - 2:
            pol_wc  = len(wtoks(fixed[i]))
            orig_wc = len(wtoks(chunk_texts[i]))
            # Is next block shifted? (looks like orig[i+2] not orig[i+1])
            sim_same = sim(fixed[i + 1], chunk_texts[i + 1])
            sim_next = sim(fixed[i + 1], chunk_texts[i + 2]) if i + 2 < n else 0.0
            if (pol_wc >= orig_wc               # current block has same or more words (merge adds ≥1 word)
                    and sim_next > sim_same + 0.3   # next looks like orig[i+2]
                    and sim_next > 0.5):             # strong enough match
                # Revert merged block to original, reinsert orphaned block
                fixed[i] = chunk_texts[i]
                fixed.insert(i + 1, chunk_texts[i + 1])
                fixed = fixed[:n]               # trim duplicate tail
                fixes += 1
                i += 2                          # skip over the just-fixed pair
            else:
                i += 1
        total += fixes
        if fixes == 0:
            break

    return fixed, total


def _polish_chunk(api_key: str, model: str, script_snippet: str,
                  chunk_texts: list[str], api_provider: str = "laozhang",
                  google_api_key: str = "") -> list[str]:
    """Send one batch of subtitle lines to AI and return corrected list."""

    if api_provider == "google":
        # ── Google GenAI (native SDK) ────────────────────────────────────────
        gkey = google_api_key or os.environ.get("GOOGLE_API_KEY", "")
        if not gkey:
            raise RuntimeError("Google API key not set — pass google_api_key or set GOOGLE_API_KEY env var")
        try:
            from google import genai as _gai
            from google.genai import types as _gtypes
        except ImportError:
            raise RuntimeError("google-genai not installed — run: pip install google-genai")
        gclient = _gai.Client(api_key=gkey)
    else:
        # ── LaoZhang / OpenAI-compatible ────────────────────────────────────
        from openai import OpenAI as _OAI
        client = _OAI(api_key=api_key, base_url=EMBED_API_BASE)

    system = (
        "You are a professional subtitle editor and proofreader. "
        "Your job is to correct subtitle text by cross-referencing the provided script.\n\n"

        "━━━ CRITICAL STRUCTURAL RULE (read this first) ━━━\n"
        "You will receive a JSON array of subtitle blocks, e.g. [block_0, block_1, ..., block_N].\n"
        "Your output MUST be a JSON array of EXACTLY the same length.\n"
        "output[i] corrects ONLY the text inside input[i] — nothing else.\n\n"
        "FORBIDDEN — these will corrupt the entire file:\n"
        "  ✗ Moving text from input[i] into output[i-1] or output[i+1]\n"
        "  ✗ Borrowing text from a neighboring block to 'complete' a word\n"
        "  ✗ Merging two blocks into one (even if a word is split across blocks)\n"
        "  ✗ Splitting one block into two\n"
        "  ✗ Shifting any text forward or backward in the array\n\n"
        "SPLIT-WORD RULE — the most common cause of corruption:\n"
        "  A word may be split across two consecutive blocks, e.g.\n"
        "    block[i]   = 'tidak jauh berbeda dari apa yang terjadi pada sunda'\n"
        "    block[i+1] = 'land'\n"
        "  The correct output is:\n"
        "    output[i]   = 'tidak jauh berbeda dari apa yang terjadi pada Sundaland'\n"
        "    output[i+1] = 'land'   ← leave this block unchanged (the word is already in i)\n"
        "  DO NOT empty out block[i+1] and shift block[i+2]'s text into it.\n"
        "  Each block is an isolated unit — treat it as if neighboring blocks do not exist.\n\n"

        "━━━ WHAT TO FIX ━━━\n"
        "1. Capitalization — the script is the AUTHORITY for capitalization:\n"
        "   - Capitalize proper nouns, names, places, titles as they appear in the script\n"
        "   - If a word starts a sentence in the script with a capital, capitalize it here too\n"
        "   - Do NOT lowercase a word that the script capitalizes\n"
        "2. Spelling — fix misspelled words using the script as reference\n"
        "3. Numbers vs words — convert to match the script's exact form:\n"
        "   - '12.000' → 'dua belas ribu' if script uses words\n"
        "   - '1m' or '1 m' → 'satu meter' if script uses words\n"
        "   - '100m' → 'seratus meter', '20.000' → 'dua puluh ribu', etc.\n"
        "   - Also applies in reverse: if script uses digits, match digits\n"
        "4. Abbreviations and units — spell out fully if script does: "
        "'km' → 'kilometer', 'kg' → 'kilogram', etc.\n"
        "5. Punctuation — remove trailing periods at end of subtitle lines unconditionally.\n\n"

        "━━━ STRICT RULES ━━━\n"
        "- Match the script's exact wording, spelling, and number format wherever possible\n"
        "- Do NOT add, remove, or rephrase words beyond what is confirmed by the script\n"
        "- Preserve the original language of each subtitle line\n"
        "- If a subtitle has no match in the script, still remove trailing periods\n"
        "- Return ONLY a valid JSON array of corrected strings — "
        "same length, same order, one string per input block\n"
        "- No markdown, no explanation, no extra keys\n"
        "- FINAL CHECK before responding: count your output items — "
        "it MUST equal the number of input items exactly"
    )
    user = (
        f"Script reference (use this to verify correct spelling/names):\n"
        f"---\n{script_snippet}\n---\n\n"
        f"Subtitle texts to proofread ({len(chunk_texts)} blocks) — "
        f"return a JSON array of EXACTLY {len(chunk_texts)} corrected strings, "
        f"one per input block, in the same order:\n"
        f"{json.dumps(chunk_texts, ensure_ascii=False)}"
    )

    if api_provider == "google":
        mdl = model if model.startswith("models/") else f"models/{model}"
        gresp = gclient.models.generate_content(
            model=mdl,
            contents=user,
            config=_gtypes.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=8192,
                response_mime_type="application/json",
            ),
        )
        raw = (gresp.text or "").strip()
    else:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            max_tokens=8192,
        )
        raw = resp.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```\s*$",       "", raw, flags=re.MULTILINE).strip()

    # ── JSON repair ──────────────────────────────────────────────────────────
    # 1. Extract the array portion only (drop any trailing prose)
    bracket = raw.find("[")
    if bracket > 0:
        raw = raw[bracket:]

    # 2. Try to parse as-is first
    try:
        corrected = json.loads(raw)
    except json.JSONDecodeError:
        # 3. Strip common Gemini/model artifacts: control chars except \n \t
        cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw)

        # 4. Fix unterminated last string: if array isn't closed, close it
        #    Find last complete item and close the array
        try:
            corrected = json.loads(cleaned)
        except json.JSONDecodeError:
            # 5. Truncate at last valid comma-separated item boundary and close
            last_comma = cleaned.rfind('",')
            if last_comma == -1:
                last_comma = cleaned.rfind('",\n')
            if last_comma != -1:
                truncated = cleaned[:last_comma + 2].rstrip().rstrip(",") + "\n]"
                try:
                    corrected = json.loads(truncated)
                except json.JSONDecodeError:
                    raise ValueError(f"Could not repair JSON response: {raw[:200]}")
            else:
                raise ValueError(f"Could not repair JSON response: {raw[:200]}")
    if not isinstance(corrected, list):
        raise ValueError("AI did not return a JSON array")
    # Pad or trim to match expected count rather than hard-fail
    if len(corrected) < len(chunk_texts):
        corrected += chunk_texts[len(corrected):]  # pad with originals
    elif len(corrected) > len(chunk_texts):
        corrected = corrected[:len(chunk_texts)]   # trim excess

    # ── Cascade merge fix ────────────────────────────────────────────────────
    # Models often "complete" a sentence across blocks (e.g. append "land"
    # from block i+1 onto block i to form "Sundaland"), which shifts every
    # subsequent block by one position. Iteratively detect and revert these.
    corrected, n_cascade = _fix_cascade_merges(corrected, chunk_texts)
    if n_cascade:
        print(f"  [cascade-fix] {n_cascade} merged block(s) reverted")

    return corrected


@app.post("/srt/polish")
async def srt_polish(body: dict):
    """
    Read .srt + script from the MCP folder, fix subtitle text via AI,
    return the corrected SRT as plain text.

    Body params:
      srt_path    — relative path of the .srt file  (e.g. "episode1.srt")
      script_path — relative path of the script file (e.g. "episode1.txt")
      model       — chat model ID (default: gemini-2.5-flash)
      api_key     — LaoZhang API key (falls back to env LAOZHANG_API_KEY)
      num_batches — how many batches to split into (default: 3, max: 10)
      parallel    — true = all batches run simultaneously (faster, may hit rate limit)
                    false = sequential one by one (default, safer)
    """
    if FOLDER is None:
        raise HTTPException(503, "MCP folder not set — submit a folder path first")

    srt_rel      = (body.get("srt_path")      or "").strip()
    script_rel   = (body.get("script_path")   or "").strip()
    model        = (body.get("model")          or "gemini-2.5-flash").strip()
    api_key      = (body.get("api_key")        or EMBED_API_KEY     ).strip()
    api_provider = (body.get("api_provider")   or "laozhang"        ).strip()
    google_api_key = (body.get("google_api_key") or os.environ.get("GOOGLE_API_KEY", "")).strip()
    num_batches  = max(1, min(10, int(body.get("num_batches") or 3)))
    parallel     = bool(body.get("parallel", False))

    if not srt_rel or not script_rel:
        raise HTTPException(400, "srt_path and script_path are required")
    if not api_key:
        raise HTTPException(400, "api_key required (set LAOZHANG_API_KEY or pass api_key)")

    srt_path    = (FOLDER / srt_rel).resolve()
    script_path = (FOLDER / script_rel).resolve()

    if not srt_path.exists():
        raise HTTPException(404, f"SRT not found: {srt_rel}")
    if not script_path.exists():
        raise HTTPException(404, f"Script not found: {script_rel}")

    srt_text    = srt_path.read_text(encoding="utf-8", errors="replace")
    script_text = script_path.read_text(encoding="utf-8", errors="replace")
    script_len  = len(script_text)

    blocks = _parse_srt(srt_text)
    if not blocks:
        raise HTTPException(400, "Could not parse SRT — check file format")

    n          = len(blocks)
    batch_size = math.ceil(n / num_batches)

    # Build batch plans
    batch_plans = []
    for batch_idx in range(num_batches):
        start = batch_idx * batch_size
        end   = min(start + batch_size, n)
        if start >= n:
            break
        overlap    = max(200, int(script_len * 0.05))
        win_start  = max(0,          int((start / n) * script_len) - overlap)
        win_end    = min(script_len, int((end   / n) * script_len) + overlap)
        batch_plans.append({
            "idx":        batch_idx,
            "start":      start,
            "end":        end,
            "texts":      [b["text"] for b in blocks[start:end]],
            "script_win": script_text[win_start:win_end],
        })

    # Start with originals — failed batches keep the original text
    corrected_texts: list[str] = [b["text"] for b in blocks]
    batch_errors: list[str]    = []

    if parallel:
        import asyncio
        loop = asyncio.get_event_loop()

        async def run_batch(plan):
            try:
                fixed = await loop.run_in_executor(
                    None, _polish_chunk,
                    api_key, model, plan["script_win"], plan["texts"],
                    api_provider, google_api_key
                )
                return plan["idx"], plan["start"], fixed, None
            except Exception as e:
                return plan["idx"], plan["start"], None, str(e)

        results = await asyncio.gather(*[run_batch(p) for p in batch_plans])

        for batch_idx, start, fixed, err in results:
            plan = batch_plans[batch_idx]
            end  = plan["end"]
            if err:
                batch_errors.append(
                    f"batch {batch_idx + 1}/{num_batches} "
                    f"(blocks {start + 1}–{end}) failed: {err}"
                )
            else:
                for i, text in enumerate(fixed):
                    corrected_texts[start + i] = text

    else:
        for plan in batch_plans:
            start = plan["start"]
            end   = plan["end"]
            try:
                fixed = _polish_chunk(api_key, model, plan["script_win"], plan["texts"],
                                      api_provider, google_api_key)
                for i, text in enumerate(fixed):
                    corrected_texts[start + i] = text
            except Exception as e:
                batch_errors.append(
                    f"batch {plan['idx'] + 1}/{num_batches} "
                    f"(blocks {start + 1}–{end}) failed: {e}"
                )

    # Rebuild SRT with corrected text, timestamps untouched
    for i, block in enumerate(blocks):
        block["text"] = corrected_texts[i]

    corrected_srt = _rebuild_srt(blocks)

    original_texts = [b["text"] for b in _parse_srt(srt_text)]
    changes = sum(1 for a, b in zip(original_texts, corrected_texts) if a != b)

    return {
        "ok":           True,
        "blocks":       n,
        "batches":      num_batches,
        "parallel":     parallel,
        "changes":      changes,
        "batch_errors": batch_errors,
        "srt":          corrected_srt,
    }


@app.get("/mode")
async def get_mode():
    """Show current search mode and available options."""
    return {
        "current_mode": SEARCH_MODE,
        "available_modes": {
            "bm25":   "Keyword matching — fast, no deps, works offline",
            "local":  f"Local embeddings ({EMBED_MODEL_LOCAL}) — best multilingual, ~2GB model",
            "api":    f"API embeddings ({EMBED_MODEL_API} via laozhang.ai) — no local model needed",
            "hybrid": "BM25 + semantic via RRF fusion — best overall quality",
        },
        "api_key_set": bool(EMBED_API_KEY),
        "cache_exists": os.path.exists(CACHE_FILE),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LaoZhang MCP Folder Reader v3")
    parser.add_argument("--folder",  "-f", required=True,
                        help="Path to folder (e.g. ~/Downloads)")
    parser.add_argument("--port",    "-p", type=int, default=8001)
    parser.add_argument("--top-k",   type=int, default=TOP_K_CHUNKS,
                        help="Max chunks returned per context query")
    parser.add_argument("--search",  "-s",
                        choices=["bm25", "local", "api", "hybrid"],
                        default=os.environ.get("SEARCH_MODE", "bm25"),
                        help=(
                            "Search mode:\n"
                            "  bm25   — keyword matching (default, no extra deps)\n"
                            "  local  — BAAI/bge-m3 local model (multilingual, ~2GB)\n"
                            "  api    — OpenAI embeddings via laozhang.ai\n"
                            "  hybrid — BM25 + semantic fusion (recommended)"
                        ))
    parser.add_argument("--embed-model", default=None,
                        help="Override embedding model name (for --search api)")
    parser.add_argument("--api-key", default=None,
                        help="laozhang.ai API key (overrides LAOZHANG_API_KEY env var)")
    parser.add_argument("--clear-cache", action="store_true",
                        help="Delete the embedding cache before starting")
    args = parser.parse_args()

    # Apply runtime config
    TOP_K_CHUNKS = args.top_k
    SEARCH_MODE  = args.search
    FOLDER       = Path(args.folder).expanduser().resolve()

    if args.api_key:
        EMBED_API_KEY = args.api_key
    if args.embed_model:
        if SEARCH_MODE == "api":
            EMBED_MODEL_API   = args.embed_model
        else:
            EMBED_MODEL_LOCAL = args.embed_model

    if not FOLDER.exists():
        print(f"Error: folder not found: {FOLDER}")
        sys.exit(1)

    if args.clear_cache and os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)
        print(f"  Cache cleared: {CACHE_FILE}")

    # Validate API mode requirements
    if SEARCH_MODE in ("api", "hybrid") and not EMBED_API_KEY:
        print("Warning: --search api/hybrid requires LAOZHANG_API_KEY env var.")
        print("  Falling back to local embeddings for semantic part.")

    files = get_all_files()
    print(f"\nLaoZhang MCP Folder Reader v3")
    print(f"  Folder      : {FOLDER}")
    print(f"  Files       : {len(files)} readable files")
    print(f"  Search mode : {SEARCH_MODE}")
    if SEARCH_MODE == "api":
        print(f"  Embed model : {EMBED_MODEL_API} (via laozhang.ai)")
    elif SEARCH_MODE in ("local", "hybrid"):
        print(f"  Embed model : {EMBED_MODEL_LOCAL} (local)")
    print(f"  URL         : http://127.0.0.1:{args.port}")
    print(f"  Building index…\n")
    build_index()
    print()

    uvicorn.run(app, host="0.0.0.0", port=args.port)
