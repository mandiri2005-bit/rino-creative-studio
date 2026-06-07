"""
LaoZhang MCP — Native Stdio Server  (Opsi B · ~99% Real Anthropic MCP)

This is a REAL MCP server implementing the Model Context Protocol specification:
  - JSON-RPC 2.0 over stdio transport
  - Full capability negotiation handshake
  - Native tool definitions (list_tools / call_tool)
  - Native resource exposure (list_resources / read_resource)
  - Native prompts (list_prompts / get_prompt)
  - Compatible with Claude Desktop, Claude Code, and any MCP client

Unlike mcp_files.py (REST sidecar), this server is launched as a subprocess
by the MCP client (e.g. laozhang_api.py or Claude Desktop) and communicates
via stdin/stdout — no HTTP, no port, no uvicorn.

Run standalone for testing:
  python mcp_files_stdio.py --folder ~/Downloads --search hybrid

Register in laozhang_api.py or Claude Desktop config:
  {
    "mcpServers": {
      "laozhang-files": {
        "command": "python",
        "args": ["mcp_files_stdio.py", "--folder", "~/Downloads", "--search", "hybrid"]
      }
    }
  }

Install deps:
  pip install mcp anthropic                          # MCP protocol
  pip install sentence-transformers numpy            # for local/hybrid search
  pip install openai                                 # for api search
  pip install pymupdf                                # for PDF support
"""

from __future__ import annotations

import os
import re
import sys
import json
import math
import pickle
import hashlib
import asyncio
import argparse
import logging
from pathlib import Path
from typing import Any, Optional

import numpy as np

# ---------------------------------------------------------------------------
# Logging — MUST go to stderr only. stdout is the MCP JSON-RPC channel.
# ---------------------------------------------------------------------------
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mcp-files")

# ---------------------------------------------------------------------------
# Config
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
CACHE_FILE        = ".mcp_stdio_cache.pkl"

EMBED_API_BASE    = "https://api.laozhang.ai/v1"
EMBED_API_KEY     = os.environ.get("LAOZHANG_API_KEY", "")
EMBED_MODEL_API   = "text-embedding-3-small"
EMBED_MODEL_LOCAL = "BAAI/bge-m3"

# Runtime state (set in main before server starts)
FOLDER: Path     = None
SEARCH_MODE: str = "bm25"

_index: list[dict] = []
_index_built       = False
_local_model       = None

# ---------------------------------------------------------------------------
# ── File helpers ────────────────────────────────────────────────────────────
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
                "name":    fname,
                "path":    str(fpath.relative_to(FOLDER)),
                "ext":     fpath.suffix.lower(),
                "size_kb": round(size / 1024, 1),
            })
    return result


def safe_read(rel_path: str) -> str:
    fpath = (FOLDER / rel_path).resolve()
    if not str(fpath).startswith(str(FOLDER.resolve())):
        raise PermissionError(f"Access denied: {rel_path}")
    if not fpath.exists():
        raise FileNotFoundError(f"File not found: {rel_path}")
    size_mb = fpath.stat().st_size / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise ValueError(f"File too large (max {MAX_FILE_SIZE_MB} MB): {rel_path}")
    if fpath.suffix.lower() == ".pdf":
        try:
            import fitz
            doc = fitz.open(str(fpath))
            return "\n".join(page.get_text() for page in doc)
        except ImportError:
            raise ImportError("pymupdf not installed — run: pip install pymupdf")
    return fpath.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# ── Chunking ────────────────────────────────────────────────────────────────
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
# ── BM25 ────────────────────────────────────────────────────────────────────
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
# ── Embeddings ──────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------
def _load_local_model():
    global _local_model
    if _local_model is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise RuntimeError("Run: pip install sentence-transformers")
        log.info(f"Loading local model: {EMBED_MODEL_LOCAL}")
        _local_model = SentenceTransformer(EMBED_MODEL_LOCAL)
        log.info("Local model ready.")
    return _local_model


def embed_local(texts: list[str]) -> np.ndarray:
    return _load_local_model().encode(
        texts, batch_size=32,
        show_progress_bar=len(texts) > 100,
        normalize_embeddings=True,
    )


def embed_api(texts: list[str]) -> np.ndarray:
    from openai import OpenAI
    client = OpenAI(api_key=EMBED_API_KEY, base_url=EMBED_API_BASE)
    all_embs = []
    for i in range(0, len(texts), 512):
        resp = client.embeddings.create(model=EMBED_MODEL_API, input=texts[i:i+512])
        all_embs.extend(item.embedding for item in sorted(resp.data, key=lambda x: x.index))
    matrix = np.array(all_embs, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.maximum(norms, 1e-9)


def embed_texts(texts: list[str]) -> np.ndarray:
    if SEARCH_MODE in ("api",) and EMBED_API_KEY:
        return embed_api(texts)
    return embed_local(texts)


def cosine_bulk(q: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    return matrix @ q


# ---------------------------------------------------------------------------
# ── Index ───────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------
def _folder_hash() -> str:
    files = get_all_files()
    return hashlib.md5(str([(f["path"], f["size_kb"]) for f in files]).encode()).hexdigest()


def _load_cache() -> bool:
    if SEARCH_MODE == "bm25" or not os.path.exists(CACHE_FILE):
        return False
    try:
        with open(CACHE_FILE, "rb") as f:
            cached = pickle.load(f)
        if (cached.get("folder_hash") == _folder_hash()
                and cached.get("search_mode") == SEARCH_MODE):
            _index.extend(cached["index"])
            log.info(f"Cache hit — {len(_index)} chunks loaded")
            return True
    except Exception as e:
        log.warning(f"Cache load failed: {e}")
    return False


def _save_cache():
    try:
        with open(CACHE_FILE, "wb") as f:
            pickle.dump({
                "folder_hash": _folder_hash(),
                "search_mode": SEARCH_MODE,
                "index": _index,
            }, f)
        log.info(f"Cache saved → {CACHE_FILE}")
    except Exception as e:
        log.warning(f"Cache save failed: {e}")


def build_index():
    global _index_built
    _index.clear()

    if _load_cache():
        _index_built = True
        return

    all_chunks = []
    for f in get_all_files():
        try:
            content = safe_read(f["path"])
            all_chunks.extend(chunk_text(content, f["path"]))
        except Exception as e:
            log.warning(f"Skip {f['path']}: {e}")

    _index.extend(all_chunks)
    log.info(f"Chunked {len(get_all_files())} files → {len(_index)} chunks")

    if SEARCH_MODE != "bm25":
        log.info(f"Embedding {len(_index)} chunks…")
        texts = [c["text"] for c in _index]
        embs  = embed_texts(texts)
        for i, emb in enumerate(embs):
            _index[i]["embedding"] = emb
        _save_cache()

    _index_built = True
    log.info(f"Index ready · mode={SEARCH_MODE} · chunks={len(_index)}")


# ---------------------------------------------------------------------------
# ── Search ──────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------
def semantic_search(query: str, top_k: int = TOP_K_CHUNKS) -> list[dict]:
    q_vec = embed_texts([query])[0]
    matrix = np.stack([c["embedding"] for c in _index])
    scores = cosine_bulk(q_vec, matrix)
    top_idx = np.argsort(scores)[::-1][:top_k]
    return [_index[i] for i in top_idx]


def hybrid_search(query: str, top_k: int = TOP_K_CHUNKS) -> list[dict]:
    K = 60
    sem  = semantic_search(query, top_k * 2)
    bm25 = bm25_search(query, top_k * 2)
    scores: dict[tuple, float] = {}
    for rank, c in enumerate(sem):
        k = (c["path"], c["chunk_idx"]); scores[k] = scores.get(k, 0) + 1/(K+rank)
    for rank, c in enumerate(bm25):
        k = (c["path"], c["chunk_idx"]); scores[k] = scores.get(k, 0) + 1/(K+rank)
    sorted_keys = sorted(scores, key=scores.get, reverse=True)[:top_k]
    idx_map = {(c["path"], c["chunk_idx"]): c for c in _index}
    return [idx_map[k] for k in sorted_keys if k in idx_map]


def search_chunks(query: str, top_k: int = TOP_K_CHUNKS) -> list[dict]:
    if not _index_built:
        build_index()
    if not query.strip():
        return _index[:top_k]
    if SEARCH_MODE == "bm25":
        return bm25_search(query, top_k)
    elif SEARCH_MODE == "hybrid":
        return hybrid_search(query, top_k)
    return semantic_search(query, top_k)


def format_chunks(chunks: list[dict]) -> str:
    by_file: dict[str, list] = {}
    for c in chunks:
        by_file.setdefault(c["path"], []).append(c)
    for p in by_file:
        by_file[p].sort(key=lambda c: c["chunk_idx"])
    parts, total = [], 0
    for p, cs in by_file.items():
        header = f"=== {p} ===\n"
        body   = "\n…\n".join(c["text"] for c in cs)
        block  = header + body + "\n"
        if total + len(block) > MAX_CONTEXT_CHARS:
            remaining = MAX_CONTEXT_CHARS - total
            if remaining > 200:
                parts.append(block[:remaining] + "\n[truncated]")
            break
        parts.append(block)
        total += len(block)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# ── MCP JSON-RPC 2.0 Protocol Implementation ────────────────────────────────
# ---------------------------------------------------------------------------
SERVER_INFO = {
    "name":    "laozhang-files",
    "version": "4.0.0",
}

CAPABILITIES = {
    "tools":     {"listChanged": False},
    "resources": {"listChanged": False, "subscribe": False},
    "prompts":   {"listChanged": False},
    "logging":   {},
}

TOOLS = [
    {
        "name":        "search_files",
        "description": (
            "Search the user's local folder for relevant content using "
            "semantic + keyword hybrid search (BM25 + vector embeddings). "
            "Returns the most relevant excerpts from matching files. "
            "Call this when the user's question relates to their documents, "
            "code, notes, or any local files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type":        "string",
                    "description": "What to search for — natural language or keywords",
                },
                "paths": {
                    "type":        "string",
                    "description": "Optional comma-separated file paths to restrict search. Leave empty for all files.",
                    "default":     "",
                },
                "top_k": {
                    "type":        "integer",
                    "description": "Max number of chunks to return (default 25)",
                    "default":     25,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name":        "read_file",
        "description": "Read the complete content of a specific file from the user's indexed folder.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type":        "string",
                    "description": "Relative path to the file within the indexed folder",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name":        "list_files",
        "description": "List all files available in the user's indexed folder with their sizes.",
        "inputSchema": {
            "type":       "object",
            "properties": {},
        },
    },
    {
        "name":        "get_index_info",
        "description": "Get information about the current search index: folder path, file count, chunk count, search mode.",
        "inputSchema": {
            "type":       "object",
            "properties": {},
        },
    },
]

PROMPTS = [
    {
        "name":        "summarize_folder",
        "description": "Generate a summary of all files in the indexed folder",
        "arguments":   [],
    },
    {
        "name":        "find_and_explain",
        "description": "Search for a topic and explain what the files say about it",
        "arguments":   [
            {"name": "topic", "description": "Topic to search for", "required": True},
        ],
    },
]


# ── Tool executor ────────────────────────────────────────────────────────────
def execute_tool(name: str, args: dict) -> list[dict]:
    """Execute a tool and return MCP content blocks."""
    try:
        if name == "search_files":
            query  = args.get("query", "")
            paths  = args.get("paths", "")
            top_k  = int(args.get("top_k", TOP_K_CHUNKS))

            if not _index_built:
                build_index()

            # Filter by paths if provided
            working = _index
            if paths:
                path_set = {p.strip() for p in paths.split(",") if p.strip()}
                working = [c for c in _index if c["path"] in path_set]

            # Temporarily swap global index for filtered search
            original = _index[:]
            _index.clear()
            _index.extend(working)
            try:
                chunks = search_chunks(query, top_k)
            finally:
                _index.clear()
                _index.extend(original)

            if not chunks:
                text = "[No relevant content found]"
            else:
                text = format_chunks(chunks)
                text = f"[Search: '{query}' · mode={SEARCH_MODE} · {len(chunks)} chunks]\n\n{text}"

            return [{"type": "text", "text": text}]

        elif name == "read_file":
            path    = args.get("path", "")
            content = safe_read(path)
            return [{"type": "text", "text": f"=== {path} ===\n{content}"}]

        elif name == "list_files":
            files = get_all_files()
            if not files:
                return [{"type": "text", "text": "[No files indexed]"}]
            lines = [f"Folder: {FOLDER}  ({len(files)} files)\n"]
            for f in files:
                lines.append(f"  {f['path']}  ({f['size_kb']} KB)")
            return [{"type": "text", "text": "\n".join(lines)}]

        elif name == "get_index_info":
            return [{"type": "text", "text": json.dumps({
                "folder":       str(FOLDER),
                "files":        len(get_all_files()),
                "chunks":       len(_index),
                "search_mode":  SEARCH_MODE,
                "index_built":  _index_built,
                "embed_model":  EMBED_MODEL_API if SEARCH_MODE == "api" else EMBED_MODEL_LOCAL,
            }, indent=2)}]

        else:
            return [{"type": "text", "text": f"[Unknown tool: {name}]"}]

    except FileNotFoundError as e:
        return [{"type": "text", "text": f"[Error: {e}]"}]
    except PermissionError as e:
        return [{"type": "text", "text": f"[Access denied: {e}]"}]
    except Exception as e:
        log.error(f"Tool {name} error: {e}")
        return [{"type": "text", "text": f"[Tool error: {e}]"}]


# ── Resource helpers ─────────────────────────────────────────────────────────
def get_resources() -> list[dict]:
    """Expose each file as an MCP resource."""
    resources = []
    for f in get_all_files():
        mime = {
            ".md": "text/markdown", ".py": "text/x-python",
            ".js": "text/javascript", ".ts": "text/typescript",
            ".json": "application/json", ".csv": "text/csv",
            ".html": "text/html", ".txt": "text/plain",
        }.get(f["ext"], "text/plain")
        resources.append({
            "uri":      f"file://laozhang/{f['path']}",
            "name":     f["name"],
            "mimeType": mime,
        })
    return resources


def read_resource(uri: str) -> str:
    prefix = "file://laozhang/"
    if not uri.startswith(prefix):
        raise ValueError(f"Unknown URI scheme: {uri}")
    rel_path = uri[len(prefix):]
    return safe_read(rel_path)


def get_prompt(name: str, args: dict) -> dict:
    if name == "summarize_folder":
        files = get_all_files()
        file_list = "\n".join(f"  - {f['path']} ({f['size_kb']} KB)" for f in files[:50])
        return {
            "description": "Summarize the indexed folder",
            "messages": [{
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        f"I have {len(files)} files indexed in my folder at {FOLDER}.\n\n"
                        f"Files:\n{file_list}\n\n"
                        "Please use the search_files and read_file tools to explore these files "
                        "and provide a comprehensive summary of their contents and themes."
                    )
                }
            }]
        }
    elif name == "find_and_explain":
        topic = args.get("topic", "the main topic")
        return {
            "description": f"Find and explain: {topic}",
            "messages": [{
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        f"Please search my files for information about '{topic}' "
                        f"and explain what my documents say about it. "
                        f"Use the search_files tool with the query '{topic}'."
                    )
                }
            }]
        }
    raise ValueError(f"Unknown prompt: {name}")


# ---------------------------------------------------------------------------
# ── JSON-RPC 2.0 dispatcher ─────────────────────────────────────────────────
# ---------------------------------------------------------------------------
def make_response(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def make_error(request_id: Any, code: int, message: str, data: Any = None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": err}


def dispatch(request: dict) -> Optional[dict]:
    """
    Handle one JSON-RPC request. Returns a response dict or None for notifications.
    Error codes follow JSON-RPC 2.0 spec:
      -32700 Parse error
      -32600 Invalid request
      -32601 Method not found
      -32602 Invalid params
      -32603 Internal error
    """
    req_id = request.get("id")           # None = notification (no response needed)
    method = request.get("method", "")
    params = request.get("params", {}) or {}

    log.debug(f"→ {method}  id={req_id}")

    try:
        # ── Lifecycle ──────────────────────────────────────────────────────
        if method == "initialize":
            return make_response(req_id, {
                "protocolVersion": "2024-11-05",
                "serverInfo":      SERVER_INFO,
                "capabilities":    CAPABILITIES,
            })

        if method == "notifications/initialized":
            # Client acknowledged init — start building index in background
            log.info("Client initialized — building index…")
            return None   # notification, no response

        if method == "ping":
            return make_response(req_id, {})

        # ── Tools ──────────────────────────────────────────────────────────
        if method == "tools/list":
            return make_response(req_id, {"tools": TOOLS})

        if method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {}) or {}
            content   = execute_tool(tool_name, tool_args)
            return make_response(req_id, {
                "content": content,
                "isError": False,
            })

        # ── Resources ──────────────────────────────────────────────────────
        if method == "resources/list":
            return make_response(req_id, {"resources": get_resources()})

        if method == "resources/read":
            uri     = params.get("uri", "")
            content = read_resource(uri)
            return make_response(req_id, {
                "contents": [{"uri": uri, "mimeType": "text/plain", "text": content}]
            })

        # ── Prompts ────────────────────────────────────────────────────────
        if method == "prompts/list":
            return make_response(req_id, {"prompts": PROMPTS})

        if method == "prompts/get":
            name   = params.get("name", "")
            p_args = params.get("arguments", {}) or {}
            result = get_prompt(name, p_args)
            return make_response(req_id, result)

        # ── Logging ────────────────────────────────────────────────────────
        if method == "logging/setLevel":
            level = params.get("level", "info").upper()
            logging.getLogger().setLevel(getattr(logging, level, logging.INFO))
            return make_response(req_id, {})

        # ── Unknown ────────────────────────────────────────────────────────
        if req_id is not None:
            return make_error(req_id, -32601, f"Method not found: {method}")
        return None

    except ValueError as e:
        return make_error(req_id, -32602, str(e))
    except Exception as e:
        log.error(f"Internal error handling {method}: {e}")
        return make_error(req_id, -32603, f"Internal error: {e}")


# ---------------------------------------------------------------------------
# ── Stdio transport ─────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------
async def stdio_server():
    """
    Read newline-delimited JSON from stdin, write responses to stdout.
    This is the MCP stdio transport — Claude connects to this process
    via subprocess and communicates via stdin/stdout pipes.
    """
    loop   = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    proto  = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: proto, sys.stdin)

    # Wrap stdout for async writing
    w_transport, w_proto = await loop.connect_write_pipe(
        asyncio.BaseProtocol, sys.stdout.buffer
    )
    writer = asyncio.StreamWriter(w_transport, w_proto, reader, loop)

    log.info(f"MCP stdio server ready · folder={FOLDER} · mode={SEARCH_MODE}")

    async def send(obj: dict):
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        writer.write(line.encode("utf-8"))
        await writer.drain()

    while True:
        try:
            raw = await reader.readline()
        except Exception:
            break

        if not raw:
            break

        raw = raw.strip()
        if not raw:
            continue

        try:
            request = json.loads(raw)
        except json.JSONDecodeError as e:
            await send(make_error(None, -32700, f"Parse error: {e}"))
            continue

        response = dispatch(request)

        # Fire-and-forget: after initialized notification, build index
        if request.get("method") == "notifications/initialized":
            asyncio.ensure_future(_build_index_async())

        if response is not None:
            await send(response)


async def _build_index_async():
    """Build the search index in a thread so it doesn't block the event loop."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, build_index)


# ---------------------------------------------------------------------------
# ── Entry point ─────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------
def main():
    global FOLDER, SEARCH_MODE, TOP_K_CHUNKS, EMBED_MODEL_API, EMBED_MODEL_LOCAL, EMBED_API_KEY

    parser = argparse.ArgumentParser(
        description="LaoZhang MCP — Native Stdio Server (Opsi B · ~99% Real MCP)"
    )
    parser.add_argument("--folder",  "-f", required=True,
                        help="Path to folder to index (e.g. ~/Downloads)")
    parser.add_argument("--search",  "-s",
                        choices=["bm25", "local", "api", "hybrid"],
                        default=os.environ.get("SEARCH_MODE", "bm25"),
                        help="Search mode (default: bm25)")
    parser.add_argument("--top-k",   type=int, default=TOP_K_CHUNKS)
    parser.add_argument("--embed-model", default=None)
    parser.add_argument("--clear-cache", action="store_true")
    args = parser.parse_args()

    FOLDER       = Path(args.folder).expanduser().resolve()
    SEARCH_MODE  = args.search
    TOP_K_CHUNKS = args.top_k

    if args.embed_model:
        if SEARCH_MODE == "api":
            EMBED_MODEL_API   = args.embed_model
        else:
            EMBED_MODEL_LOCAL = args.embed_model

    if not FOLDER.exists():
        log.error(f"Folder not found: {FOLDER}")
        sys.exit(1)

    if args.clear_cache and os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)
        log.info(f"Cache cleared: {CACHE_FILE}")

    log.info(f"LaoZhang MCP Stdio Server v4.0")
    log.info(f"  Folder : {FOLDER}")
    log.info(f"  Files  : {len(get_all_files())} readable files")
    log.info(f"  Mode   : {SEARCH_MODE}")
    log.info(f"  Top-K  : {TOP_K_CHUNKS}")

    # Run async stdio server
    try:
        asyncio.run(stdio_server())
    except KeyboardInterrupt:
        log.info("Server stopped.")


if __name__ == "__main__":
    main()
