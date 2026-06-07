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
import math
import pickle
import hashlib
import argparse
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
