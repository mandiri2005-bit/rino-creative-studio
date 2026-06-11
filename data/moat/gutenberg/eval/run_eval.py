#!/usr/bin/env python3
"""
run_eval.py — STEP 0.3
==============================================================================
Runs the frozen golden set through the LIVE retrieval + generation paths and
dumps the raw results to a timestamped JSON. NO scoring here — this only
*captures* what the system does, so the scorers (0.5, 0.6) and the baseline
freeze (0.4) have something to read.

TWO THINGS ARE CAPTURED PER ENTRY
------------------------------------------------------------------------------
1. RETRIEVAL  (for recall@k, scored in 0.5)
   Production-faithful retrieval, but widened and UNGATED so recall@k sees the
   full ranked candidate list:
     - filters from get_style_config(style): style_filter / structure_filter /
       min_quality / query_instruction   (same as production)
     - prefer_source='rino' when the entry asks for it (the two-pass rino-first
       logic lives in get_narration_context — we don't reimplement it)
     - top_k widened (default 10) and min_score=0.0 so the 0.55 gate does NOT
       trim the list  (the gate is a generation-time decision, not a retrieval-
       quality one — keeping it out makes recall@k measure ranking stability)
   -> records [{passage_id, score, source}, ...] per entry.

2. OUTPUT  (for the 8.5 rubric, scored in 0.6)
   The two outputs are RAG-on vs STANDARD (RAG-off), via the existing
   compare_rag_vs_standard() — this IS the "compare_rag_vs_standard CLI" Step 0
   promotes. (There is no separate native-Google *narration* path in the
   codebase; narration generation is single-provider via LaoZhang. _generate_google
   in laozhang_api.py is image-only.) For rino entries we set RAG_PREFER_SOURCE
   so the RAG path grounds on proprietary content, matching production.
   -> records {"rag": {...}, "standard": {...}} per entry.

OUTPUT FILE: results/run_YYYYMMDD_HHMMSS.json
  { "meta": {...}, "entries": [ {id, topic, style, lang, prefer_source,
    "retrieved": [...], "output": {"rag": {...}, "standard": {...}}, "error": null}, ...] }

Reuse the live functions — retrieval and generation are NOT reimplemented here.

Usage:
    python run_eval.py                      # full 25-entry run
    python run_eval.py --limit 1            # smoke test on the first entry
    python run_eval.py --top-k 10 --duration 2 --model gemini-2.5-flash
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent

# the embedding model / Qdrant collection retrieval runs against (production)
EMBED_MODEL_KEY = "qwen3-0.6b"

# --- live pipeline (imported lazily so --help works outside the project) ------
# eval/ sits inside the gutenberg project dir; we add the parent at run time so
# the live modules import cleanly whether run from the project root or eval/.
configure_model = COLLECTION = None
get_narration_context = compare_rag_vs_standard = get_style_config = None


def _import_pipeline() -> None:
    """Import the live retrieval + generation functions into module globals.
    Deferred so `python run_eval.py --help` works without the project on path."""
    global configure_model, COLLECTION
    global get_narration_context, compare_rag_vs_standard, get_style_config
    sys.path.insert(0, str(HERE.parent))
    try:
        from qdrant_index_v2 import configure_model, COLLECTION
        from rag_narration import get_narration_context, compare_rag_vs_standard
        from style_rag_config import get_style_config
    except Exception as exc:
        print(f"[run_eval] FATAL: could not import the live pipeline: {exc}")
        print("  Run this from the gutenberg project dir (the one with rag_narration.py),")
        print("  with the project's venv active and Qdrant reachable.")
        raise


# ---------------------------------------------------------------------------
# Golden set
# ---------------------------------------------------------------------------
REQUIRED_FIELDS = ("id", "topic", "style", "lang", "prefer_source")


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_golden(path: Path) -> list[dict]:
    """Load + lightly validate golden_set.yaml -> list of entry dicts."""
    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = data.get("entries") if isinstance(data, dict) else None
    if not entries:
        raise SystemExit(f"[run_eval] no 'entries' found in {path}")
    seen = set()
    for e in entries:
        missing = [f for f in REQUIRED_FIELDS if f not in e]
        if missing:
            raise SystemExit(f"[run_eval] entry {e.get('id','?')!r} missing fields: {missing}")
        if e["id"] in seen:
            raise SystemExit(f"[run_eval] duplicate id: {e['id']!r}")
        seen.add(e["id"])
    return entries


# ---------------------------------------------------------------------------
# Per-entry capture
# ---------------------------------------------------------------------------
async def capture_retrieval(entry: dict, top_k: int) -> list[dict]:
    """Production-faithful, widened, UNGATED retrieval -> [{passage_id, score, source}]."""
    cfg = get_style_config(entry["style"])
    ctx = await get_narration_context(
        topic=entry["topic"],
        style=cfg["style_filter"],
        structure=cfg["structure_filter"],
        min_quality=cfg["min_quality"],
        top_k=top_k,                                   # widen vs prod for recall@k
        query_instruction=cfg["query_instruction"],
        prefer_source="rino" if entry["prefer_source"] else None,
        min_score=0.0,                                 # disable the 0.55 gate for capture
    )
    out = []
    for p in ctx.get("passages", []):
        out.append({
            "passage_id": p.get("passage_id"),
            "score":      p.get("score"),
            "source":     p.get("source"),
        })
    return out


async def capture_output(entry: dict, duration: int, model: str, api_key: str) -> dict:
    """RAG-on vs standard via the existing A/B helper. prefer_source is read from
    RAG_PREFER_SOURCE env by generate_rag_narration, so set it per entry."""
    prev = os.environ.get("RAG_PREFER_SOURCE")
    os.environ["RAG_PREFER_SOURCE"] = "rino" if entry["prefer_source"] else ""
    try:
        res = await compare_rag_vs_standard(
            topic=entry["topic"],
            style=entry["style"],
            duration_minutes=duration,
            language=entry["lang"],
            model=model,
            api_key=api_key,
        )
    finally:
        if prev is None:
            os.environ.pop("RAG_PREFER_SOURCE", None)
        else:
            os.environ["RAG_PREFER_SOURCE"] = prev

    def slim(r: dict) -> dict:
        return {
            "narration":          r.get("narration", ""),
            "sources":            r.get("sources", []),
            "passages_retrieved": r.get("passages_retrieved", 0),
            "rag_used":           r.get("rag_used", False),
        }

    return {"rag": slim(res["rag"]), "standard": slim(res["standard"])}


async def run_entry(entry: dict, top_k: int, duration: int,
                    model: str, api_key: str) -> dict:
    """Capture retrieval + dual-path output for one entry (errors captured, not raised)."""
    row = {
        "id":            entry["id"],
        "topic":         entry["topic"],
        "style":         entry["style"],
        "lang":          entry["lang"],
        "prefer_source": entry["prefer_source"],
        "retrieved":     [],
        "output":        {"rag": None, "standard": None},
        "error":         None,
    }
    try:
        row["retrieved"] = await capture_retrieval(entry, top_k)
        row["output"] = await capture_output(entry, duration, model, api_key)
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    return row


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
async def run_all(args: argparse.Namespace) -> Path:
    _import_pipeline()
    entries = load_golden(args.golden)
    if args.limit:
        entries = entries[: args.limit]

    configure_model(EMBED_MODEL_KEY)   # point retrieval at the production collection

    print(f"[run_eval] {len(entries)} entries | embed={EMBED_MODEL_KEY} "
          f"coll={COLLECTION} | gen={args.model} dur={args.duration}m top_k={args.top_k}")

    rows = []
    t0 = time.time()
    for i, entry in enumerate(entries, 1):
        tag = "rino" if entry["prefer_source"] else "corp"
        print(f"  [{i:>2}/{len(entries)}] {entry['id']:<32} {entry['style']:<22} {tag} ... ",
              end="", flush=True)
        te = time.time()
        row = await run_entry(entry, args.top_k, args.duration, args.model, args.api_key)
        rows.append(row)
        if row["error"]:
            print(f"ERROR ({time.time()-te:.1f}s): {row['error']}")
        else:
            nret = len(row["retrieved"])
            rw = len((row["output"]["rag"] or {}).get("narration", "").split())
            sw = len((row["output"]["standard"] or {}).get("narration", "").split())
            print(f"ok  ret={nret:<2} rag={rw}w std={sw}w  ({time.time()-te:.1f}s)")

    n_err = sum(1 for r in rows if r["error"])
    payload = {
        "meta": {
            "created_at":       datetime.now().isoformat(timespec="seconds"),
            "golden_set":       args.golden.name,
            "golden_sha256":    sha256_of(args.golden),
            "embed_model":      EMBED_MODEL_KEY,
            "collection":       COLLECTION,
            "gen_model":        args.model,
            "top_k":            args.top_k,
            "duration_minutes": args.duration,
            "n_entries":        len(rows),
            "n_ok":             len(rows) - n_err,
            "n_error":          n_err,
        },
        "entries": rows,
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = args.out_dir / f"run_{stamp}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n[run_eval] wrote {out_path}")
    print(f"  ok={payload['meta']['n_ok']}  errors={n_err}  "
          f"elapsed={time.time()-t0:.1f}s")
    if n_err:
        print(f"  ! {n_err} entrie(s) errored — inspect before freezing a baseline.")
    return out_path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run golden set through live RAG paths (capture only).")
    p.add_argument("--golden", type=Path, default=HERE / "golden_set.yaml",
                   help="path to golden_set.yaml (default: ./golden_set.yaml)")
    p.add_argument("--out-dir", type=Path, default=HERE / "results",
                   help="where to write run_*.json (default: ./results)")
    p.add_argument("--top-k", type=int, default=10,
                   help="passages to retrieve per entry; widened vs prod for recall@k (default 10)")
    p.add_argument("--duration", type=int, default=2,
                   help="target narration duration in minutes (default 2 — keep short for eval)")
    p.add_argument("--model", default=os.environ.get("RAG_DEFAULT_MODEL", "gemini-2.5-flash"),
                   help="generation model (default gemini-2.5-flash / RAG_DEFAULT_MODEL)")
    p.add_argument("--api-key", default=os.environ.get("LAOZHANG_API_KEY", ""),
                   help="LaoZhang API key (or set LAOZHANG_API_KEY)")
    p.add_argument("--limit", type=int, default=None,
                   help="only run the first N entries (smoke test)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if not args.api_key:
        print("[run_eval] WARNING: no API key (set LAOZHANG_API_KEY or pass --api-key). "
              "Retrieval will still run; generation will fail.")
    asyncio.run(run_all(args))


if __name__ == "__main__":
    main()
